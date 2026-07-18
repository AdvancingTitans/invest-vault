import { invoke } from "@tauri-apps/api/core";
import { openUrl } from "@tauri-apps/plugin-opener";
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

type AssetType = "a_share" | "hk_stock" | "fund";
type Holding = {
  security_id: string;
  name: string;
  symbol: string;
  asset_type: AssetType;
  invested_amount_cny: string | null;
  bought_on: string | null;
  quantity: string | null;
  price: number | null;
  change_percent: number | null;
  trade_date: string | null;
  data_session: "盘前" | "盘中" | "盘后";
  data_label: string;
  valuation_status: string;
  source: string;
  estimated_profit_cny: number | null;
  estimated_profit_percent: number | null;
  profit_basis: string | null;
  profit_reason: string | null;
};
type HoldingEntry = {
  holding_id: string;
  security_id: string;
  asset_type: AssetType;
  invested_amount_cny: string;
  bought_on: string;
  revision_number: string;
  estimated_profit_cny: number | null;
  estimated_profit_percent: number | null;
  profit_reason: string | null;
};
type Material = {
  material_id: string;
  security_id: string;
  material_type: string;
  title: string;
  published_at: string;
  source_name: string;
  source_url: string;
  excerpt: string;
};
type Note = {
  note_id: string;
  security_id: string;
  body: string;
  created_at: string;
  updated_at?: string;
  source_title: string | null;
  source_url: string | null;
  quoted_text: string | null;
};
type FinancialPeriod = {
  period: string;
  period_label: string;
  roe: number | null;
  gross_margin: number | null;
  debt_asset_ratio: number | null;
  operating_cash_flow: number | null;
  free_cash_flow: number | null;
};
type Financials = {
  periods: FinancialPeriod[];
  source: string;
  cutoff_date: string;
  free_cash_flow_note: string;
};
type FundProfile = {
  symbol: string;
  name: string;
  cutoff_date: string;
  source: string;
  nav_history: Array<{
    date: string;
    nav: number;
    change_percent: number | null;
    event: string | null;
  }>;
  returns: Record<string, number>;
  fees: {
    management_rate: string | null;
    custodian_rate: string | null;
    sales_service_rate: string | null;
  };
  managers: Array<{
    name: string;
    work_time: string;
    managed_scale: string;
    score: number | null;
    tenure_return_percent: number | null;
  }>;
};
type Thesis = {
  thesis_id: string;
  body: string;
  review_due_on: string | null;
  revision_number: number;
};
type Workspace = {
  thesis: Thesis | null;
  materials: Material[];
  notes: Note[];
  financials: Financials | null;
  fund: FundProfile | null;
  timeline: {
    items: Array<{ event_id: string; summary: string; occurred_at: string }>;
    total_count: number;
  };
};
type Market = {
  report_stage?: MarketReportStage;
  indices?: {
    date: string;
    session: "盘前" | "盘中" | "盘后";
    session_label: string;
    observed_at: string;
    source: string;
    rows: Array<{
      code: string;
      name: string;
      market: "CN" | "HK" | "US";
      currency: "CNY" | "HKD" | "USD";
      trade_date: string;
      close: number;
      change: number | null;
      change_percent: number | null;
      volume: number | null;
      amount: number | null;
      session: "盘前" | "盘中" | "盘后";
    }>;
  };
  lhb?: {
    date: string;
    source: string;
    rows: Array<{
      symbol: string;
      name: string;
      change_percent: number | null;
      buy_amount: number | null;
      sell_amount: number | null;
      net_amount: number | null;
      reason: string;
    }>;
  };
  industry_flow?: {
    date: string;
    source: string;
    inbound: Array<{
      code: string;
      name: string;
      change_percent: number | null;
      net_amount: number | null;
      net_ratio: number | null;
    }>;
    outbound: Array<{
      code: string;
      name: string;
      change_percent: number | null;
      net_amount: number | null;
      net_ratio: number | null;
    }>;
  };
  market_news?: {
    date: string;
    observed_at: string;
    window_hours: number;
    total_count: number;
    source: string;
    items: Array<{
      region: "A股" | "港股" | "美股";
      title: string;
      published_at: string;
      url: string;
      source: string;
    }>;
  };
  pulse?: {
    date: string;
    session: "盘前" | "盘中" | "盘后";
    kind: "limit_pools" | "holding_news";
    source: string;
    skill_version: string;
    news?: Array<{ symbol: string; name: string; title: string; published_at: string; url: string; source: string }>;
    m3?: {
      available: boolean;
      limit_up_count: number;
      first_board_count: number;
      multi_board_count: number;
      seal_fund_yi: number;
      top_themes: Array<{ name: string; count: number }>;
      leaders: Array<{ symbol: string; name: string; theme: string; board_days: number; seal_fund_yi: number }>;
    };
    m4?: {
      available: boolean;
      limit_down_count: number;
      failed_breakout_count: number;
      failed_breakout_ratio: number | null;
      rows: Array<{ symbol: string; name: string; theme: string; change_percent: number; kind: string }>;
    };
  };
};
type MarketReportStage = {
  session: "盘前" | "盘中" | "盘后";
  report_date: string;
  is_trade_day: boolean;
  label: string;
};
type Bootstrap = {
  mode: "vault";
  report_as_of: string | null;
  refreshed_at: string | null;
  archive_coverage: {
    target_date: string | null;
    current: number;
    total: number;
    stale: number;
  };
  disclaimer: string;
  holdings: Holding[];
  holding_entries: HoldingEntry[];
  portfolio_profile: {
    cash_balance_cny: string;
    max_drawdown_percent: string | null;
  };
  market: Market;
};
type HoldingDraft = {
  row_id: string;
  symbol: string;
  asset_type: AssetType;
  invested_amount_cny: string;
  bought_on: string;
};
type DeleteRequest =
  | { kind: "holding"; entry: HoldingEntry; label: string }
  | { kind: "note"; note: Note; label: string };
type AIStatus = {
  available: boolean;
  authenticated: boolean;
  provider: string;
  detail: string;
  account?: { type: string; email?: string; planType?: string } | null;
};
type AIModel = {
  id: string;
  displayName: string;
  description?: string;
  isDefault?: boolean;
  defaultReasoningEffort?: string;
  supportedReasoningEfforts?: Array<string | { reasoningEffort: string; description?: string }>;
};
type AIModelTask = "quick_note" | "research" | "committee";
type AISettings = {
  provider: "codex_app_server";
  tasks: Record<AIModelTask, { model_id: string | null; reasoning_effort: string | null }>;
};
type QuickNoteShape = {
  title: string;
  facts: string[];
  user_judgements: string[];
  open_questions: string[];
  planned_actions: string[];
  tags: string[];
};
type QuickNoteDraft = {
  draft_id: string;
  security_id: string;
  raw_text: string;
  draft: QuickNoteShape;
  status: "draft";
  created_at: string;
};
type AIRole = {
  role_id: string;
  name: string;
  focus: string;
  questions: string;
  risk_focus: string;
};
type ChatMode = "assistant" | "committee";
type ChatThread = {
  thread_id: string;
  thread_type: ChatMode;
  title: string;
  security_id: string;
  role_id: string;
  updated_at: string;
};
type ChatSource = { name: string; url: string; as_of: string };
type ChatEvent = {
  event_id: string;
  event_type: string;
  actor_type: "user" | "assistant" | "system";
  actor_id: string;
  payload: {
    content: string;
    role_name?: string;
    skill_name?: string;
    cited_evidence_ids?: string[];
    sources?: ChatSource[];
    assumptions?: string[];
    unknowns?: string[];
    materials?: Array<{
      title: string;
      published_at: string;
      source_name: string;
      source_url: string;
    }>;
    gaps?: string[];
    evidence_ids?: string[];
    refused?: boolean;
    selected_roles?: string[];
    assignments?: Array<{ name: string; function: string }>;
    reason?: string;
    stages?: string[];
    report?: boolean;
    suggested_mode?: ChatMode;
  };
};
type ChatRun = {
  run_id: string;
  status: "running" | "completed" | "failed";
  current_stage: string;
  started_at: string;
  completed_at?: string | null;
};
type ChatDetail = ChatThread & {
  events: ChatEvent[];
  active_run?: ChatRun | null;
};

const navItems = [
  { key: "today", label: "今日复盘" },
  { key: "market", label: "市场概览" },
  { key: "portfolio", label: "持仓账本" },
  { key: "security", label: "证券资料" },
  { key: "research", label: "投资笔记" },
  { key: "data", label: "数据与备份" },
  { key: "settings", label: "设置" },
];
async function api<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, options);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `请求失败（${response.status}）`);
  }
  return response.json();
}
function cleanAssistantText(value: string): string {
  return value
    .replace(
      /[（(]?EVIDENCE(?:-(?:SKILL|FINANCIAL|FUND))?-[A-Za-z0-9_-]+[）)]?/g,
      "",
    )
    .replace(/\*\*(.*?)\*\*/g, "$1")
    .replace(/^#{1,6}\s*/gm, "")
    .replace(/^\s*[-*]\s+/gm, "• ")
    .trim();
}

function normalizeSearch(text: string): string {
  return text.toLocaleLowerCase("zh-CN").replace(/[\s·，。,:：;；/\\_\-]+/g, "");
}

function fuzzyMatch(value: string, query: string): boolean {
  const haystack = normalizeSearch(value);
  const needle = normalizeSearch(query);
  if (!needle || haystack.includes(needle)) return true;
  let cursor = 0;
  for (const character of haystack) {
    if (character === needle[cursor]) cursor += 1;
    if (cursor === needle.length) return true;
  }
  return false;
}

function isSecurityCodeQuery(query: string): boolean {
  return /^\d{5,6}$/.test(query.trim());
}

function securityCode(securityId: string): string {
  return securityId.split(":")[2] ?? "";
}

function plainMarkdown(text: string): string {
  return text
    .replace(/^#{1,6}\s+/gm, "")
    .replace(/^>\s?/gm, "")
    .replace(/^\s*[-*]\s+/gm, "")
    .replace(/^\s*\d+[.)]\s+/gm, "")
    .replace(/\*\*(.*?)\*\*/g, "$1")
    .replace(/\*(.*?)\*/g, "$1")
    .replace(/==(.+?)==/g, "$1")
    .replace(/\s+/g, " ")
    .trim();
}

function renderInlineMarkdown(line: string, keyPrefix = "inline"): React.ReactNode[] {
  return line
    .split(/(\*\*\*.+?\*\*\*|\*\*[^*]+?\*\*|\*[^*]+?\*|==.*?==)/g)
    .filter(Boolean)
    .map((part, index) => {
      const key = `${keyPrefix}-${index}`;
      if (part.startsWith("***") && part.endsWith("***")) {
        const content = part.slice(3, -3);
        return <strong key={key}><em>{renderInlineMarkdown(content, `${key}-both`)}</em></strong>;
      }
      if ((part.startsWith("**") && part.endsWith("**")) ||
        (part.startsWith("==") && part.endsWith("=="))) {
        const content = part.slice(2, -2);
        return <strong key={key}>{renderInlineMarkdown(content, `${key}-strong`)}</strong>;
      }
      if (part.startsWith("*") && part.endsWith("*")) {
        const content = part.slice(1, -1);
        return <em key={key}>{renderInlineMarkdown(content, `${key}-em`)}</em>;
      }
      return <span key={key}>{part.replaceAll("**", "").replaceAll("==", "")}</span>;
    });
}

function RichText({ text }: { text: string }) {
  const inline = (line: string) =>
    renderInlineMarkdown(line);
  const tableCells = (line: string) =>
    line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim());
  const lines = text.split("\n");
  const blocks: React.ReactNode[] = [];
  for (let index = 0; index < lines.length;) {
    const line = lines[index];
    const next = lines[index + 1] ?? "";
    const isTable = line.includes("|") && next.includes("|") &&
      tableCells(next).every((cell) => /^:?-{3,}:?$/.test(cell));
    if (isTable) {
      const headings = tableCells(line);
      const rows: string[][] = [];
      index += 2;
      while (index < lines.length && lines[index].includes("|")) {
        rows.push(tableCells(lines[index]));
        index += 1;
      }
      blocks.push(
        <div className="table-wrap markdown-table" key={`table-${index}`}>
          <table className="data-table">
            <thead><tr>{headings.map((cell, cellIndex) => <th key={cellIndex}>{inline(cell)}</th>)}</tr></thead>
            <tbody>{rows.map((row, rowIndex) => (
              <tr key={rowIndex}>{headings.map((_heading, cellIndex) => <td key={cellIndex}>{inline(row[cellIndex] ?? "")}</td>)}</tr>
            ))}</tbody>
          </table>
        </div>,
      );
      continue;
    }
    const quote = line.match(/^>\s?(.*)$/);
    if (quote) {
      const quoted: string[] = [];
      while (index < lines.length) {
        const match = lines[index].match(/^>\s?(.*)$/);
        if (!match) break;
        quoted.push(match[1]);
        index += 1;
      }
      blocks.push(
        <blockquote className="quoted-text" key={`quote-${index}`}>
          {quoted.map((item, itemIndex) => <p key={itemIndex}>{inline(item)}</p>)}
        </blockquote>,
      );
      continue;
    }
    const unordered = line.match(/^\s*[-*]\s+(.*)$/);
    if (unordered) {
      const items: string[] = [];
      while (index < lines.length) {
        const match = lines[index].match(/^\s*[-*]\s+(.*)$/);
        if (!match) break;
        items.push(match[1]);
        index += 1;
      }
      blocks.push(
        <ul className="report-list" key={`list-${index}`}>
          {items.map((item, itemIndex) => <li key={itemIndex}>{inline(item)}</li>)}
        </ul>,
      );
      continue;
    }
    const ordered = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (ordered) {
      const items: string[] = [];
      while (index < lines.length) {
        const match = lines[index].match(/^\s*\d+[.)]\s+(.*)$/);
        if (!match) break;
        items.push(match[1]);
        index += 1;
      }
      blocks.push(
        <ol className="report-list" key={`ordered-list-${index}`}>
          {items.map((item, itemIndex) => <li key={itemIndex}>{inline(item)}</li>)}
        </ol>,
      );
      continue;
    }
    index += 1;
    if (!line.trim()) continue;
    const heading = line.match(/^#{1,6}\s+(.*)$/);
    if (heading) {
      blocks.push(<h4 key={`line-${index}`}>{inline(heading[1])}</h4>);
      continue;
    }
    blocks.push(
      <p key={`line-${index}`}>{inline(line)}</p>,
    );
  }
  return (
    <div className="rich-text">{blocks}</div>
  );
}
const money = (value: number | null) =>
  value === null
    ? "—"
    : `¥${value.toLocaleString("zh-CN", { maximumFractionDigits: 2 })}`;
const amountYi = (value: number | null) =>
  value === null
    ? "—"
    : `${(value / 100_000_000).toLocaleString("zh-CN", { maximumFractionDigits: 2 })}亿`;
const percent = (value: number | null) =>
  value === null ? "—" : `${value >= 0 ? "+" : ""}${value.toFixed(2)}%`;
const day = (value: string | null) => (value ? value.slice(0, 10) : "尚未归档");
const assetLabel = (value: AssetType) =>
  ({ a_share: "A股", hk_stock: "港股", fund: "基金" })[value];
const invested = (value: string | null) =>
  value === null
    ? "—"
    : `¥${Number(value).toLocaleString("zh-CN", { maximumFractionDigits: 2 })}`;
const symbolOf = (securityId: string) => securityId.split(":")[2];
const tone = (value: number | null) =>
  value === null ? "" : value >= 0 ? "up" : "down";
const newDraft = (): HoldingDraft => ({
  row_id: globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`,
  symbol: "",
  asset_type: "a_share",
  invested_amount_cny: "",
  bought_on: "",
});
const followSource = (
  event: React.MouseEvent<HTMLAnchorElement>,
  url: string,
) => {
  if (
    (window as unknown as { __TAURI_INTERNALS__?: object }).__TAURI_INTERNALS__
  ) {
    event.preventDefault();
    void openUrl(url);
  }
};
const openExternal = async (url: string) => {
  if (
    (window as unknown as { __TAURI_INTERNALS__?: object }).__TAURI_INTERNALS__
  )
    await openUrl(url);
  else window.open(url, "_blank", "noopener,noreferrer");
};
const quickNoteBody = (draft: QuickNoteShape) =>
  [
    [draft.title],
    draft.facts.length
      ? ["事实（来自原始速记）", ...draft.facts.map((item) => `- ${item}`)]
      : [],
    draft.user_judgements.length
      ? ["我的判断", ...draft.user_judgements.map((item) => `- ${item}`)]
      : [],
    draft.open_questions.length
      ? ["待验证", ...draft.open_questions.map((item) => `- ${item}`)]
      : [],
    draft.planned_actions.length
      ? ["计划", ...draft.planned_actions.map((item) => `- ${item}`)]
      : [],
    draft.tags.length ? [`标签：${draft.tags.join("、")}`] : [],
  ]
    .filter((section) => section.length)
    .map((section) => section.join("\n"))
    .join("\n\n");

function Tag({
  children,
  tone = "neutral",
}: {
  children: React.ReactNode;
  tone?: "neutral" | "up" | "down" | "warning";
}) {
  return <span className={`tag tag-${tone}`}>{children}</span>;
}
function Card({
  title,
  children,
  action,
  className = "",
  style,
}: {
  title: string;
  children: React.ReactNode;
  action?: React.ReactNode;
  className?: string;
  style?: React.CSSProperties;
}) {
  return (
    <section className={`card ${className}`} style={style}>
      <div className="card-header">
        <h2>{title}</h2>
        {action && <div className="card-action">{action}</div>}
      </div>
      <div className="card-body">{children}</div>
    </section>
  );
}
function PageHeader({
  eyebrow,
  title,
  description,
  action,
}: {
  eyebrow: string;
  title: string;
  description: string;
  action?: React.ReactNode;
}) {
  return (
    <header className="page-header">
      <div className="page-eyebrow">{eyebrow}</div>
      <div className="page-header-title-row">
        <h1>{title}</h1>
        {action}
      </div>
      <p>{description}</p>
    </header>
  );
}
const cardLimitForWidth = (width: number) =>
  width >= 960 ? 12 : width >= 720 ? 8 : width >= 540 ? 4 : 3;
const replaceTrailingCards = (
  current: string[],
  selected: string[],
  limit: number,
) => {
  const promoted = [...new Set(selected)].slice(0, limit);
  const keep = current
    .filter((id) => !promoted.includes(id))
    .slice(0, Math.max(0, limit - promoted.length));
  return [...keep, ...promoted];
};

function HoldingEditor({
  save,
  cancel,
  initialRows,
  editing = false,
}: {
  save: (rows: HoldingDraft[]) => Promise<void>;
  cancel: () => void;
  initialRows?: HoldingDraft[];
  editing?: boolean;
}) {
  const [rows, setRows] = useState<HoldingDraft[]>(
    initialRows?.length ? initialRows : [newDraft()],
  );
  const [errors, setErrors] = useState<Record<string, string>>({});
  const today = new Date().toISOString().slice(0, 10);
  const update = (rowId: string, field: keyof HoldingDraft, value: string) => {
    setRows((current) =>
      current.map((row) =>
        row.row_id === rowId ? { ...row, [field]: value } : row,
      ),
    );
    setErrors((current) => {
      const next = { ...current };
      delete next[`${rowId}:${field}`];
      return next;
    });
  };
  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    const next: Record<string, string> = {};
    rows.forEach((row) => {
      const symbol = row.symbol.trim().toUpperCase();
      if (!symbol) next[`${row.row_id}:symbol`] = "请填写证券代码";
      else if (
        ["a_share", "fund"].includes(row.asset_type) &&
        !/^\d{6}$/.test(symbol)
      )
        next[`${row.row_id}:symbol`] = "A股和基金代码为6位数字";
      else if (row.asset_type === "hk_stock" && !/^\d{1,5}$/.test(symbol))
        next[`${row.row_id}:symbol`] = "港股代码为1至5位数字";
      if (!row.invested_amount_cny || Number(row.invested_amount_cny) <= 0)
        next[`${row.row_id}:invested_amount_cny`] = "请填写大于0的人民币金额";
      if (!row.bought_on) next[`${row.row_id}:bought_on`] = "请选择买入日期";
      else if (row.bought_on > today)
        next[`${row.row_id}:bought_on`] = "买入日期不能晚于今天";
    });
    setErrors(next);
    if (!Object.keys(next).length)
      await save(
        rows.map((row) => ({
          ...row,
          symbol: row.symbol.trim().toUpperCase(),
        })),
      );
  };
  return (
    <>
      <PageHeader
        eyebrow={editing ? "修订持仓" : "持仓录入"}
        title={editing ? "编辑持仓记录" : "添加我的持仓"}
        description="金额只按人民币记录；盈亏使用买入日收盘价或基金净值估算，不推算持仓数量。"
      />
      <form className="holding-editor" onSubmit={submit} noValidate>
        <div className="holding-editor-head">
          <div>
            <strong>{editing ? "核对修订内容" : "持仓清单"}</strong>
            <span>代码、类型、金额、日期均为必填</span>
          </div>
          <button type="button" className="text-button" onClick={cancel}>
            返回
          </button>
        </div>
        <div className="holding-entry-list">
          {rows.map((row, index) => (
            <fieldset className="holding-entry-row" key={row.row_id}>
              <legend>第 {index + 1} 行</legend>
              <label>
                <span>
                  代码 <b>*</b>
                </span>
                <input
                  className="mono"
                  value={row.symbol}
                  onChange={(event) =>
                    update(row.row_id, "symbol", event.target.value)
                  }
                  placeholder="600519"
                  aria-invalid={Boolean(errors[`${row.row_id}:symbol`])}
                />
                {errors[`${row.row_id}:symbol`] && (
                  <small>{errors[`${row.row_id}:symbol`]}</small>
                )}
              </label>
              <label>
                <span>
                  类型 <b>*</b>
                </span>
                <select
                  value={row.asset_type}
                  onChange={(event) =>
                    update(
                      row.row_id,
                      "asset_type",
                      event.target.value as AssetType,
                    )
                  }
                >
                  <option value="a_share">A股</option>
                  <option value="hk_stock">港股</option>
                  <option value="fund">基金</option>
                </select>
              </label>
              <label>
                <span>
                  买入金额（人民币）<b>*</b>
                </span>
                <input
                  className="mono"
                  type="number"
                  min="0.01"
                  step="0.01"
                  value={row.invested_amount_cny}
                  onChange={(event) =>
                    update(
                      row.row_id,
                      "invested_amount_cny",
                      event.target.value,
                    )
                  }
                  aria-invalid={Boolean(
                    errors[`${row.row_id}:invested_amount_cny`],
                  )}
                />
                {errors[`${row.row_id}:invested_amount_cny`] && (
                  <small>{errors[`${row.row_id}:invested_amount_cny`]}</small>
                )}
              </label>
              <label>
                <span>
                  买入日期 <b>*</b>
                </span>
                <input
                  className="mono"
                  type="date"
                  max={today}
                  value={row.bought_on}
                  onChange={(event) =>
                    update(row.row_id, "bought_on", event.target.value)
                  }
                  aria-invalid={Boolean(errors[`${row.row_id}:bought_on`])}
                />
                {errors[`${row.row_id}:bought_on`] && (
                  <small>{errors[`${row.row_id}:bought_on`]}</small>
                )}
              </label>
              {!editing && rows.length > 1 && (
                <div className="holding-entry-remove">
                  <span>本行操作</span>
                  <button
                    type="button"
                    className="text-button danger-text"
                    onClick={() =>
                      setRows((current) =>
                        current.filter((item) => item.row_id !== row.row_id),
                      )
                    }
                    aria-label={`删除第 ${index + 1} 行`}
                  >
                    删除本行
                  </button>
                </div>
              )}
            </fieldset>
          ))}
        </div>
        <div className="holding-editor-actions">
          {!editing && (
            <button
              type="button"
              className="secondary"
              disabled={rows.length >= 100}
              onClick={() => setRows((current) => [...current, newDraft()])}
            >
              ＋ 添加下一行
            </button>
          )}
          <button type="submit">{editing ? "保存修订" : "确认并保存"}</button>
        </div>
      </form>
    </>
  );
}

function HoldingPicker({
  holdings,
  visibleIds,
  limit,
  cancel,
  apply,
}: {
  holdings: Holding[];
  visibleIds: string[];
  limit: number;
  cancel: () => void;
  apply: (ids: string[]) => void;
}) {
  const [selected, setSelected] = useState<string[]>([]);
  const cancelRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    cancelRef.current?.focus();
    const close = (event: KeyboardEvent) => {
      if (event.key === "Escape") cancel();
    };
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, [cancel]);
  return (
    <div className="modal-backdrop">
      <section
        className="holding-picker"
        role="dialog"
        aria-modal="true"
        aria-labelledby="holding-picker-title"
      >
        <span className="confirm-kicker">首页卡片</span>
        <h2 id="holding-picker-title">选择展示持仓</h2>
        <p>
          勾选未展示的标的；确认后，选中 {selected.length} 张将替换当前末尾{" "}
          {Math.min(selected.length, visibleIds.length)} 张。
        </p>
        <div className="holding-picker-list">
          {holdings.map((item) => {
            const visible = visibleIds.includes(item.security_id);
            const checked = visible || selected.includes(item.security_id);
            return (
              <label
                key={item.security_id}
                className={visible ? "is-visible" : ""}
              >
                <input
                  type="checkbox"
                  checked={checked}
                  disabled={visible || (!checked && selected.length >= limit)}
                  onChange={(event) =>
                    setSelected((current) =>
                      event.target.checked
                        ? [...current, item.security_id]
                        : current.filter((id) => id !== item.security_id),
                    )
                  }
                />
                <span>
                  <strong>{item.name}</strong>
                  <small className="mono">
                    {item.symbol} · {assetLabel(item.asset_type)}
                  </small>
                </span>
                <em>
                  {visible
                    ? "当前展示"
                    : selected.includes(item.security_id)
                      ? "待替换"
                      : "可选择"}
                </em>
              </label>
            );
          })}
        </div>
        <div className="confirm-actions">
          <button ref={cancelRef} className="secondary" onClick={cancel}>
            取消
          </button>
          <button disabled={!selected.length} onClick={() => apply(selected)}>
            确认替换 {selected.length} 张
          </button>
        </div>
      </section>
    </div>
  );
}

function HoldingDeck({
  holdings,
  openHolding,
  archiveDate,
}: {
  holdings: Holding[];
  openHolding: (id: string) => void;
  archiveDate: string | null;
}) {
  const deckRef = useRef<HTMLDivElement>(null);
  const pointerDrag = useRef<{
    sourceId: string;
    pointerId: number;
    startX: number;
    startY: number;
    active: boolean;
    lastTargetId: string | null;
  } | null>(null);
  const suppressOpen = useRef(false);
  const [draggingId, setDraggingId] = useState<string | null>(null);
  const [limit, setLimit] = useState(3);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [dismissedIds, setDismissedIds] = useState<string[]>([]);
  const [visibleIds, setVisibleIds] = useState<string[] | null>(() => {
    try {
      const saved = localStorage.getItem("holding-card-slots");
      return saved ? JSON.parse(saved) : null;
    } catch {
      return null;
    }
  });
  useEffect(() => {
    if (!deckRef.current) return;
    const observer = new ResizeObserver(([entry]) =>
      setLimit(cardLimitForWidth(entry.contentRect.width)),
    );
    observer.observe(deckRef.current);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    setVisibleIds((current) => {
      const valid = (current ?? [])
        .filter((id) => holdings.some((item) => item.security_id === id))
        .slice(0, limit);
      for (const item of holdings)
        if (valid.length < limit && !valid.includes(item.security_id))
          valid.push(item.security_id);
      return valid;
    });
  }, [holdings, limit]);
  useEffect(() => {
    if (visibleIds)
      localStorage.setItem("holding-card-slots", JSON.stringify(visibleIds));
  }, [visibleIds]);
  const moveCard = (sourceId: string, targetId: string) =>
    setVisibleIds((current) => {
      const next = [...(current ?? [])];
      const source = next.indexOf(sourceId);
      const target = next.indexOf(targetId);
      if (source < 0 || target < 0 || source === target) return next;
      const [moved] = next.splice(source, 1);
      next.splice(target, 0, moved);
      return next;
    });
  const startPointerDrag = (
    event: React.PointerEvent<HTMLElement>,
    sourceId: string,
  ) => {
    if (
      event.button !== 0 ||
      (event.target as HTMLElement).closest(".holding-card-remove")
    )
      return;
    event.currentTarget.setPointerCapture(event.pointerId);
    pointerDrag.current = {
      sourceId,
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      active: false,
      lastTargetId: null,
    };
  };
  const movePointerDrag = (event: React.PointerEvent<HTMLElement>) => {
    const drag = pointerDrag.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    if (
      !drag.active &&
      Math.hypot(event.clientX - drag.startX, event.clientY - drag.startY) < 6
    )
      return;
    if (!drag.active) {
      drag.active = true;
      setDraggingId(drag.sourceId);
    }
    event.preventDefault();
    const targetId = document
      .elementFromPoint(event.clientX, event.clientY)
      ?.closest<HTMLElement>(".holding-card")?.dataset.securityId;
    if (
      targetId &&
      targetId !== drag.sourceId &&
      targetId !== drag.lastTargetId
    ) {
      moveCard(drag.sourceId, targetId);
      drag.lastTargetId = targetId;
    }
  };
  const finishPointerDrag = (event: React.PointerEvent<HTMLElement>) => {
    const drag = pointerDrag.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    if (event.currentTarget.hasPointerCapture(event.pointerId))
      event.currentTarget.releasePointerCapture(event.pointerId);
    suppressOpen.current = drag.active;
    if (drag.active)
      window.setTimeout(() => {
        suppressOpen.current = false;
      }, 0);
    pointerDrag.current = null;
    setDraggingId(null);
  };
  const visible = (visibleIds ?? [])
    .map((id) => holdings.find((item) => item.security_id === id))
    .filter((item): item is Holding => Boolean(item));
  const unshown = holdings.filter(
    (item) => !(visibleIds ?? []).includes(item.security_id),
  );
  const next =
    unshown.find((item) => !dismissedIds.includes(item.security_id)) ??
    unshown[0];
  return (
    <div className="holding-deck" ref={deckRef}>
      <div className="holding-grid">
        {visible.map((item) => (
          <article
            className={
              draggingId === item.security_id
                ? "holding-card dragging"
                : "holding-card"
            }
            data-security-id={item.security_id}
            key={item.security_id}
            onPointerDown={(event) => startPointerDrag(event, item.security_id)}
            onPointerMove={movePointerDrag}
            onPointerUp={finishPointerDrag}
            onPointerCancel={finishPointerDrag}
          >
            <button
              className="holding-card-main"
              onClick={() => {
                if (suppressOpen.current) {
                  suppressOpen.current = false;
                  return;
                }
                openHolding(item.security_id);
              }}
            >
              <div className="card-top">
                <div className="name" title={item.name}>
                  {item.name}
                </div>
                <div className="holding-card-meta">
                  <Tag>{item.symbol}</Tag>
                  <span className="sub">
                    {assetLabel(item.asset_type)} · 买入{" "}
                    {invested(item.invested_amount_cny)}
                  </span>
                </div>
              </div>
              <div className="profit">
                <span className={`amount ${tone(item.estimated_profit_cny)}`}>
                  {item.estimated_profit_cny === null
                    ? "暂不可估算"
                    : money(item.estimated_profit_cny)}
                </span>
                {item.estimated_profit_percent !== null && (
                  <span className={`percent ${tone(item.estimated_profit_percent)}`}>
                    {percent(item.estimated_profit_percent)}
                  </span>
                )}
              </div>
              <div className="footer">
                <span>{item.data_label}</span>
                <span
                  className={`status-pill ${item.trade_date !== archiveDate ? "stale" : "fresh"}`}
                >
                  {item.trade_date !== archiveDate
                    ? "数据缺失"
                    : item.profit_basis ?? "已归档"}
                </span>
              </div>
            </button>
            <button
              className="holding-card-drag"
              onKeyDown={(event) => {
                if (
                  !event.altKey ||
                  !["ArrowLeft", "ArrowRight"].includes(event.key)
                )
                  return;
                event.preventDefault();
                const index = (visibleIds ?? []).indexOf(item.security_id);
                const target = (visibleIds ?? [])[
                  index + (event.key === "ArrowLeft" ? -1 : 1)
                ];
                if (target) moveCard(item.security_id, target);
              }}
              aria-label={`拖动调整 ${item.symbol} 的顺序，或按 Option 加左右方向键`}
            >
              ⋮⋮
            </button>
            <button
              className="holding-card-remove"
              onClick={() => {
                setVisibleIds((current) =>
                  (current ?? []).filter((id) => id !== item.security_id),
                );
                setDismissedIds((current) => [
                  ...new Set([...current, item.security_id]),
                ]);
              }}
              aria-label={`从首页移除 ${item.symbol}`}
            >
              移除
            </button>
          </article>
        ))}
      </div>
      <div className="holding-deck-actions">
        {next && visible.length < limit && (
          <button
            className="secondary"
            onClick={() => {
              setVisibleIds((current) => [
                ...(current ?? []),
                next.security_id,
              ]);
              setDismissedIds((current) =>
                current.filter((id) => id !== next.security_id),
              );
            }}
          >
            ＋ 显示其他持仓
          </button>
        )}
        {holdings.length > visible.length && (
          <button className="secondary" onClick={() => setPickerOpen(true)}>
            选择展示持仓
          </button>
        )}
      </div>
      {pickerOpen && (
        <HoldingPicker
          holdings={holdings}
          visibleIds={visibleIds ?? []}
          limit={limit}
          cancel={() => setPickerOpen(false)}
          apply={(selected) => {
            setVisibleIds((current) =>
              replaceTrailingCards(current ?? [], selected, limit),
            );
            setDismissedIds((current) =>
              current.filter((id) => !selected.includes(id)),
            );
            setPickerOpen(false);
          }}
        />
      )}
    </div>
  );
}

function ArchiveBanner({ data }: { data: Bootstrap }) {
  const allUpdated =
    data.archive_coverage.current === data.archive_coverage.total &&
    data.archive_coverage.total > 0;
  return (
    <div className="archive-banner">
      <div className="date-block">
        <span className="label">归档交易日</span>
        <strong className="mono">{day(data.report_as_of)}</strong>
      </div>
      <div className="coverage">
        <strong>
          {data.archive_coverage.current}/{data.archive_coverage.total}
        </strong>{" "}
        个标的已更新
        {data.archive_coverage.stale
          ? `，${data.archive_coverage.stale} 个当日无交易或未披露`
          : ""}
        ；整理于{" "}
        {data.refreshed_at
          ? new Date(data.refreshed_at).toLocaleString("zh-CN")
          : "等待首次归档"}
      </div>
      <span className={`status-pill ${allUpdated ? "fresh" : "stale"}`}>
        {allUpdated ? "数据已同步" : "部分数据缺失"}
      </span>
    </div>
  );
}
function EmptyVault({ addHoldings }: { addHoldings: () => void }) {
  return (
    <>
      <div className="card">
        <div className="card-body">
          <div className="empty-state">
            <div className="empty-icon">◈</div>
            <h3>先添加你的持仓</h3>
            <p>
              填写代码、类型、人民币买入金额和买入日期，应用会自动整理最近完整交易日的数据。
            </p>
            <button onClick={addHoldings}>＋ 添加持仓</button>
          </div>
        </div>
      </div>
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(3, 1fr)",
          gap: "20px",
        }}
      >
        {[
          {
            icon: "◉",
            title: "市场概览",
            desc: "收盘后手动刷新全球指数、龙虎榜与行业资金流。",
          },
          {
            icon: "◐",
            title: "证券资料",
            desc: "查看持仓对应的公告、研报、财务数据与历史判断。",
          },
          {
            icon: "✎",
            title: "投资笔记",
            desc: "记录事实、判断与待验证问题，支持从资料摘录。",
          },
        ].map((item) => (
          <div className="card" key={item.title}>
            <div className="card-body">
              <div style={{ textAlign: "center", padding: "24px 16px" }}>
                <div
                  style={{
                    width: "44px",
                    height: "44px",
                    borderRadius: "12px",
                    background: "var(--canvas)",
                    display: "grid",
                    placeItems: "center",
                    margin: "0 auto 14px",
                    color: "var(--subtle)",
                    fontSize: "20px",
                  }}
                >
                  {item.icon}
                </div>
                <h4 style={{ margin: "0 0 6px", fontSize: "14px" }}>
                  {item.title}
                </h4>
                <p style={{ margin: 0, fontSize: "12.5px", color: "var(--muted)" }}>
                  {item.desc}
                </p>
              </div>
            </div>
          </div>
        ))}
      </div>
    </>
  );
}

function Today({
  data,
  workspaces,
  addHoldings,
  navigate,
  openHolding,
}: {
  data: Bootstrap;
  workspaces: Record<string, Workspace>;
  addHoldings: () => void;
  navigate: (key: string) => void;
  openHolding: (id: string) => void;
}) {
  if (!data.holdings.length)
    return (
      <>
        <PageHeader
          eyebrow="今日复盘"
          title="持仓投资札记"
          description="围绕真实持仓保存最新行情、公司资料和你的判断。"
        />
        <EmptyVault addHoldings={addHoldings} />
      </>
    );
  const calculable = data.holdings.filter(
    (item) => item.estimated_profit_cny !== null,
  );
  const profit = calculable.reduce(
    (sum, item) => sum + (item.estimated_profit_cny ?? 0),
    0,
  );
  const materials = Object.values(workspaces)
    .flatMap((item) => item.materials)
    .sort((a, b) => b.published_at.localeCompare(a.published_at))
    .slice(0, 7);
  const notes = Object.values(workspaces)
    .flatMap((item) => item.notes)
    .sort((a, b) =>
      (b.updated_at ?? b.created_at).localeCompare(
        a.updated_at ?? a.created_at,
      ),
    )
    .slice(0, 3);
  const holdingById = Object.fromEntries(
    data.holdings.map((item) => [item.security_id, item]),
  );
  return (
    <>
      <PageHeader
        eyebrow="今日复盘"
        title="持仓行情与投资记录"
        description="进入应用自动刷新最新行情；完整交易日数据继续单独归档。"
        action={
          <button onClick={addHoldings}>＋ 添加持仓</button>
        }
      />
      <ArchiveBanner data={data} />
      <div className="today-layout">
        <Card
          title="组合行情摘要"
          action={
            <span className="meta">盈亏按买入日收盘价估算</span>
          }
        >
          <div className="summary-strip today-summary-strip">
            <div className="metric-card">
              <div className="label">持仓标的</div>
              <div className="value">{data.holdings.length}</div>
            </div>
            <div className="metric-card">
              <div className="label">当前盈亏（估算）</div>
              <div className={`value ${tone(calculable.length ? profit : null)}`}>
                {calculable.length ? money(profit) : "—"}
              </div>
              {calculable.length > 0 && (
                <div className={`delta ${tone(profit)}`}>
                  {percent(
                    data.holdings.reduce(
                      (sum, item) =>
                        sum + (item.estimated_profit_percent ?? 0),
                      0,
                    ) / calculable.length,
                  )}
                </div>
              )}
            </div>
            <div className="metric-card">
              <div className="label">可估算持仓</div>
              <div className="value">
                {calculable.length}/{data.holdings.length}
              </div>
            </div>
          </div>
          <h3 style={{ fontSize: "13px", color: "var(--muted)", margin: "0 0 12px", fontWeight: 600 }}>
            首页关注持仓
          </h3>
          <HoldingDeck
            holdings={data.holdings}
            openHolding={openHolding}
            archiveDate={data.report_as_of}
          />
          <p className="hint" style={{ marginTop: "14px" }}>
            当前区域最多展示首页卡片；悬停卡片右上角拖动可调整顺序，移除不会删除持仓。
          </p>
        </Card>
        <Card
          title="持仓事项"
          action={
            <span className="meta">最近 {materials.length} 条</span>
          }
        >
          {materials.length ? (
            <div>
              {materials.map((item) => (
                <div className="material-item" key={item.material_id}>
                  <div className="meta">
                    <Tag>{item.material_type}</Tag>
                    <time className="mono" style={{ fontSize: "12px", color: "var(--subtle)" }}>
                      {item.published_at.slice(0, 10)}
                    </time>
                  </div>
                  <a
                    href={item.source_url}
                    target="_blank"
                    rel="noreferrer"
                    onClick={(event) => followSource(event, item.source_url)}
                  >
                    {item.title}
                  </a>
                  <div className="source">{item.source_name}</div>
                </div>
              ))}
            </div>
          ) : (
            <p className="empty">最近没有已验证的持仓公告或财务报告。</p>
          )}
        </Card>
        <Card
          title="待复盘笔记"
          action={
            <button
              className="text-button"
              onClick={() => navigate("research")}
            >
              打开笔记
            </button>
          }
        >
          {notes.length ? (
            <div>
              {notes.map((item) => {
                const holding = holdingById[item.security_id];
                return (
                  <div className="note-card" key={item.note_id}>
                    <div className="note-header">
                      {(holding?.name || item.security_id === marketOverviewSubject.security_id) && (
                        <strong>{holding?.name ?? "市场概览"}</strong>
                      )}
                      <time>
                        {new Date(
                          item.updated_at ?? item.created_at,
                        ).toLocaleDateString("zh-CN")}
                      </time>
                    </div>
                    <NoteDisclosure note={item} />
                  </div>
                );
              })}
            </div>
          ) : (
            <p className="empty">还没有投资笔记。</p>
          )}
        </Card>
      </div>
    </>
  );
}

function Portfolio({
  data,
  addHoldings,
  editHolding,
  deleteHolding,
  exportExcel,
  saveRiskProfile,
  refreshData,
}: {
  data: Bootstrap;
  addHoldings: () => void;
  editHolding: (entry: HoldingEntry) => void;
  deleteHolding: (entry: HoldingEntry) => void;
  exportExcel: () => void;
  saveRiskProfile: (cashBalance: string, maxDrawdown: string) => Promise<void>;
  refreshData: () => Promise<void>;
}) {
  const [cashBalance, setCashBalance] = useState(data.portfolio_profile.cash_balance_cny);
  const [maxDrawdown, setMaxDrawdown] = useState(data.portfolio_profile.max_drawdown_percent ?? "");
  const [query, setQuery] = useState("");
  useEffect(() => {
    setCashBalance(data.portfolio_profile.cash_balance_cny);
    setMaxDrawdown(data.portfolio_profile.max_drawdown_percent ?? "");
  }, [data.portfolio_profile.cash_balance_cny, data.portfolio_profile.max_drawdown_percent]);
  const byId = Object.fromEntries(
    data.holdings.map((item) => [item.security_id, item]),
  );
  const filteredEntries = data.holding_entries.filter((entry) => {
    const item = byId[entry.security_id];
    const q = query.trim().toLowerCase();
    if (!q) return true;
    return (
      item?.name.toLowerCase().includes(q) ||
      item?.symbol.toLowerCase().includes(q) ||
      assetLabel(entry.asset_type).includes(q)
    );
  });
  return (
    <>
      <PageHeader
        eyebrow="持仓账本"
        title="我的持仓"
        description="每笔买入单独保存，可修订或删除；进入应用时刷新最新行情，完整交易日价格继续留档。"
        action={<button className="secondary" onClick={() => void refreshData()}>刷新持仓行情</button>}
      />
      <div className="portfolio-stack">
        <Card title="现金与风险约束">
        <form
          className="portfolio-risk-form"
          onSubmit={(event) => {
            event.preventDefault();
            void saveRiskProfile(cashBalance, maxDrawdown);
          }}
        >
          <label>
            <span>现金余额（人民币）</span>
            <input type="number" min="0" step="0.01" required value={cashBalance} onChange={(event) => setCashBalance(event.target.value)} />
          </label>
          <label>
            <span>最大可承受回撤（%）</span>
            <input type="number" min="0.01" max="100" step="0.01" required value={maxDrawdown} onChange={(event) => setMaxDrawdown(event.target.value)} />
          </label>
          <button type="submit">保存约束</button>
        </form>
        <p className="hint">现金变动以追加记录保存；最大回撤是你的风险约束，AI 不会自行推断。</p>
        </Card>
        {!data.holding_entries.length ? (
          <EmptyVault addHoldings={addHoldings} />
        ) : (
          <Card
          title="持仓明细"
          action={
            <div className="card-action portfolio-table-tools">
              <input
                type="text"
                placeholder="搜索代码或名称…"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
              />
              <button className="secondary" onClick={addHoldings}>
                添加持仓
              </button>
              <button onClick={exportExcel}>导出 Excel</button>
            </div>
          }
        >
          <div className="table-wrap">
            <table className="data-table holdings-table">
              <thead>
                <tr>
                  <th>证券</th>
                  <th>类型</th>
                  <th className="numeric">买入金额</th>
                  <th>买入日期</th>
                  <th className="numeric">当前盈亏（估算）</th>
                  <th>数据日期</th>
                  <th>状态</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {filteredEntries.map((entry) => {
                  const item = byId[entry.security_id];
                  const hasData = item?.trade_date === data.report_as_of;
                  return (
                    <tr key={entry.holding_id}>
                      <td>
                        <strong>
                          {item?.name ?? symbolOf(entry.security_id)}
                        </strong>
                        <small className="mono" style={{ display: "block", fontSize: "12px", color: "var(--subtle)" }}>
                          {symbolOf(entry.security_id)}
                        </small>
                      </td>
                      <td>{assetLabel(entry.asset_type)}</td>
                      <td className="numeric mono">
                        {invested(entry.invested_amount_cny)}
                      </td>
                      <td className="mono">{entry.bought_on}</td>
                      <td
                        className={`numeric mono ${tone(entry.estimated_profit_cny)}`}
                      >
                        {entry.estimated_profit_cny === null ? (
                          "—"
                        ) : (
                          <>
                            {money(entry.estimated_profit_cny)}{" "}
                            <small>
                              {percent(entry.estimated_profit_percent)}
                            </small>
                          </>
                        )}
                      </td>
                      <td className="mono">{day(item?.trade_date ?? null)}</td>
                      <td>
                        <span className={`status-pill ${hasData ? "fresh" : "stale"}`}>
                          {hasData ? "已归档" : "数据缺失"}
                        </span>
                      </td>
                      <td>
                        <div className="row-actions">
                          <button
                            className="text-button"
                            onClick={() => editHolding(entry)}
                          >
                            编辑
                          </button>
                          <button
                            className="text-button danger-text"
                            onClick={() => deleteHolding(entry)}
                          >
                            删除
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <p className="hint">
            盈亏按买入日收盘价估算，不代表实际成交成本；多笔同一证券的首页盈亏为合计值。
          </p>
          </Card>
        )}
      </div>
    </>
  );
}

const marketOverviewSubject = {
  security_id: "MARKET:GLOBAL:OVERVIEW",
  name: "市场概览",
  symbol: "GLOBAL",
};

function MarketPage({
  market,
  reload,
  saveMarketNote,
}: {
  market: Market;
  reload: () => Promise<void>;
  saveMarketNote: (body: string) => Promise<void>;
}) {
  const indices = market.indices;
  const lhb = market.lhb;
  const flow = market.industry_flow;
  const marketNews = market.market_news;
  const pulse = market.pulse;
  const [refreshing, setRefreshing] = useState<string | null>(null);
  const [refreshNotice, setRefreshNotice] = useState("");
  const refreshMarket = async (section: "all" | "indices" | "lhb" | "industry_flow" | "market_news") => {
    setRefreshing(section);
    setRefreshNotice("");
    try {
      const result = await api<{ completed: string[]; failed: Record<string, string>; report_stage: MarketReportStage }>(
        "/api/market/refresh",
        { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ section }) },
      );
      await reload();
      const failures = Object.values(result.failed);
      setRefreshNotice(
        failures.length
          ? `已更新 ${result.completed.length} 项；${failures.join("；")}`
          : "市场数据已刷新",
      );
      return result.report_stage;
    } catch (error) {
      setRefreshNotice(`刷新失败：${error instanceof Error ? error.message : String(error)}`);
    } finally {
      setRefreshing(null);
    }
    return market.report_stage;
  };
  const activity = (row: NonNullable<Market["indices"]>["rows"][number]) => {
    if (row.amount != null)
      return `成交额 ${(row.amount / 100_000_000).toLocaleString("zh-CN", { maximumFractionDigits: 2 })} 亿 ${row.currency}`;
    if (row.volume != null)
      return `成交量 ${(row.volume / 100_000_000).toLocaleString("zh-CN", { maximumFractionDigits: 2 })} 亿`;
    return "—";
  };
  const regionName = (region: string) =>
    region === "CN" ? "A股" : region === "HK" ? "港股" : "美股";
  return (
    <>
      <PageHeader
        eyebrow="市场概览"
        title="全球主要市场"
        description={indices?.session_label
          ? `当前展示 ${indices.session_label}；各市场按自身交易时段保留实时行情或最近收盘。`
          : "进入应用会自动刷新市场数据；交易时段展示实时行情，非交易日展示最近收盘。"}
        action={
          <button title="刷新全部" disabled={refreshing !== null} onClick={() => void refreshMarket("all")}>
            {refreshing === "all" ? "正在刷新…" : "刷新全部"}
          </button>
        }
      />
      {refreshNotice && <p className="market-refresh-notice" role="status">{refreshNotice}</p>}
      <div className="surface-grid market-data-grid">
        <Card
          className="market-index-card"
          title={`大盘指数概览${indices?.session_label ? ` · ${indices.session_label}` : ""}`}
          action={
            <div className="card-action-group">
              {indices && <span className="meta">{indices.date} · {indices.source}</span>}
              <button className="text-button" disabled={refreshing !== null} onClick={() => void refreshMarket("indices")}>
                {refreshing === "indices" ? "刷新中…" : "刷新"}
              </button>
            </div>
          }
        >
          {indices?.rows.length ? (
            <div className="market-region-grid">
              {(["CN", "HK", "US"] as const).map((region) => (
                <section className="market-region" key={region}>
                  <h3>
                    {regionName(region)}
                  </h3>
                  <div className="market-index-grid">
                    {indices.rows.filter((row) => row.market === region).map((row) => (
                      <div className="metric-card" key={`${row.market}-${row.code}`}>
                        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "4px" }}>
                          <span style={{ fontWeight: 650 }}>{row.name}</span>
                          <span className={`tag tag-${tone(row.change_percent)}`}>
                            {percent(row.change_percent)}
                          </span>
                        </div>
                        <div className="value" style={{ fontSize: "20px" }}>
                          {row.close.toLocaleString("zh-CN")}
                        </div>
                        <div style={{ fontSize: "11.5px", color: "var(--subtle)", marginTop: "4px" }}>
                          {activity(row)}
                        </div>
                      </div>
                    ))}
                  </div>
                </section>
              ))}
            </div>
          ) : (
            <p className="empty">正在获取最新市场行情；失败时可使用右上角刷新重试。</p>
          )}
        </Card>
        <div className="market-secondary-grid">
        <Card
          title="龙虎榜"
          action={
            <div className="card-action-group">
              {lhb && <span className="meta">{lhb.date} · {lhb.source}</span>}
              <button className="text-button" disabled={refreshing !== null} onClick={() => void refreshMarket("lhb")}>
                {refreshing === "lhb" ? "刷新中…" : "刷新"}
              </button>
            </div>
          }
        >
          {lhb?.rows.length ? (
            <div className="table-wrap">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>证券</th>
                    <th className="numeric">涨跌幅</th>
                    <th className="numeric">净额</th>
                    <th>上榜原因</th>
                  </tr>
                </thead>
                <tbody>
                  {lhb.rows.map((row, index) => (
                    <tr key={`${row.symbol}-${index}`}>
                      <td>
                        <strong>{row.name}</strong>
                        <small className="mono" style={{ display: "block", fontSize: "12px", color: "var(--subtle)" }}>{row.symbol}</small>
                      </td>
                      <td
                        className={`numeric mono ${tone(row.change_percent)}`}
                      >
                        {percent(row.change_percent)}
                      </td>
                      <td className={`numeric mono ${tone(row.net_amount)}`}>
                        {amountYi(row.net_amount)}
                      </td>
                      <td>{row.reason}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="empty">
              该交易日无可核验龙虎榜记录，或来源暂不可用。
            </p>
          )}
        </Card>
        <div className="market-side-stack">
        <Card
          title="行业资金流"
          action={
            <div className="card-action-group">
              {flow && <span className="meta">{flow.date} · {flow.source}</span>}
              <button className="text-button" disabled={refreshing !== null} onClick={() => void refreshMarket("industry_flow")}>
                {refreshing === "industry_flow" ? "刷新中…" : "刷新"}
              </button>
            </div>
          }
        >
          {flow ? (
            <div className="industry-flow-columns">
              <div>
                <h4 style={{ fontSize: "12px", color: "var(--success)", margin: "0 0 10px", fontWeight: 650 }}>净流入居前</h4>
                <div style={{ display: "grid", gap: "8px" }}>
                  {flow.inbound.map((row) => (
                    <div style={{ display: "flex", justifyContent: "space-between", gap: "8px", fontSize: "13px" }} key={row.code}>
                      <span>{row.name}</span>
                      <span className="mono down" style={{ fontWeight: 600 }}>{amountYi(row.net_amount)}</span>
                    </div>
                  ))}
                </div>
              </div>
              <div>
                <h4 style={{ fontSize: "12px", color: "var(--danger)", margin: "0 0 10px", fontWeight: 650 }}>净流出居前</h4>
                <div style={{ display: "grid", gap: "8px" }}>
                  {flow.outbound.map((row) => (
                    <div style={{ display: "flex", justifyContent: "space-between", gap: "8px", fontSize: "13px" }} key={row.code}>
                      <span>{row.name}</span>
                      <span className="mono up" style={{ fontWeight: 600 }}>{amountYi(row.net_amount)}</span>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          ) : (
            <p className="empty">尚无带目标交易日时间戳的行业资金流归档。</p>
          )}
        </Card>
        <Card
          className="market-news-card"
          title="24小时大盘新闻"
          action={
            <div className="card-action-group">
              {marketNews && (
                <span className="meta">
                  显示 {Math.min(marketNews.items.length, 6)}/{marketNews.total_count} · {marketNews.source}
                </span>
              )}
              <button className="text-button" disabled={refreshing !== null} onClick={() => void refreshMarket("market_news")}>
                {refreshing === "market_news" ? "刷新中…" : "刷新"}
              </button>
            </div>
          }
        >
          {marketNews?.items.length ? (
            <div className="market-news-list">
              {marketNews.items.slice(0, 6).map((item) => (
                <a
                  className="market-news-row"
                  key={`${item.published_at}-${item.url}`}
                  href={item.url}
                  target="_blank"
                  rel="noreferrer"
                  onClick={(event) => followSource(event, item.url)}
                >
                  <time className="mono">
                    {new Date(item.published_at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}
                  </time>
                  <Tag>{item.region}</Tag>
                  <span>{item.title}</span>
                </a>
              ))}
            </div>
          ) : (
            <p className="empty">最近24小时暂无符合大盘范围和发布时间校验的公开新闻。</p>
          )}
        </Card>
        </div>
        </div>
        {pulse?.kind === "limit_pools" && (
          <>
            <Card title="赚钱效应与上涨主线">
              {pulse.m3?.available ? (
                <>
                  <p className="pulse-summary">
                    涨停 <strong>{pulse.m3.limit_up_count}</strong> 家 · 首板 {pulse.m3.first_board_count} · 连板 {pulse.m3.multi_board_count} · 封单 {pulse.m3.seal_fund_yi.toFixed(2)} 亿
                  </p>
                  <div className="pulse-leaders">
                    {pulse.m3.leaders.slice(0, 5).map((row) => (
                      <span key={row.symbol}>{row.name}（{row.symbol}）<b>{row.board_days}板</b></span>
                    ))}
                  </div>
                </>
              ) : <p className="empty">该交易日涨停池暂不可用。</p>}
            </Card>
            <Card title="下跌风险">
              {pulse.m4?.available ? (
                <>
                  <p className="pulse-summary">
                    跌停 <strong>{pulse.m4.limit_down_count}</strong> 家 · 炸板 {pulse.m4.failed_breakout_count} 家 · 炸板率 {pulse.m4.failed_breakout_ratio == null ? "—" : `${(pulse.m4.failed_breakout_ratio * 100).toFixed(1)}%`}
                  </p>
                  <div className="pulse-leaders risk">
                    {pulse.m4.rows.slice(0, 5).map((row) => (
                      <span key={`${row.kind}-${row.symbol}`}>{row.name}（{row.symbol}）<b>{row.kind}</b></span>
                    ))}
                  </div>
                </>
              ) : <p className="empty">该交易日跌停和炸板池暂不可用。</p>}
            </Card>
          </>
        )}
        {pulse?.kind === "holding_news" && (
          <Card className="market-holding-news-card" title="持仓股票 24 小时资讯">
            <div className="holding-news-pulse">
              {pulse.news?.length ? pulse.news.map((item) => (
                <a key={`${item.symbol}-${item.title}`} href={item.url} target="_blank" rel="noreferrer" onClick={(event) => followSource(event, item.url)}>
                  <strong>{item.name}</strong><span>{item.title}</span><time>{new Date(item.published_at).toLocaleString("zh-CN")}</time>
                </a>
              )) : <p className="empty">最近 24 小时暂无可核验的持仓资讯。</p>}
            </div>
          </Card>
        )}
      </div>
      <section className="market-assistant-panel" aria-label="专家风格行情报告">
        <ResearchAssistant
          selected={marketOverviewSubject}
          scene="market"
          saveNote={saveMarketNote}
          initialMarketStage={market.report_stage}
          beforeMarketReport={() => refreshMarket("all")}
        />
      </section>
    </>
  );
}

function MaterialList({
  materials,
  onExcerpt,
  type,
}: {
  materials: Material[];
  onExcerpt: (item: Material) => void;
  type?: string;
}) {
  const rows = type
    ? materials.filter((item) => item.material_type === type)
    : materials;
  if (!rows.length)
    return <p className="empty">当前没有符合日期和证券代码校验的资料。</p>;
  return (
    <>
      <p className="count-label" style={{ marginBottom: "8px" }}>
        显示最近 {Math.min(rows.length, 10)} 条，共归档 {rows.length} 条
      </p>
      <div>
        {rows.slice(0, 10).map((item) => (
          <div className="material-item" key={item.material_id}>
            <div className="meta">
              <Tag>{item.material_type}</Tag>
              <time className="mono" style={{ fontSize: "12px", color: "var(--subtle)" }}>
                {item.published_at.slice(0, 10)}
              </time>
            </div>
            <a
              href={item.source_url}
              target="_blank"
              rel="noreferrer"
              onClick={(event) => followSource(event, item.source_url)}
            >
              {item.title}
            </a>
            <div className="source">
              {item.source_name} ·{" "}
              <button className="text-button" onClick={() => onExcerpt(item)} style={{ minHeight: 0, height: "auto" }}>
                摘录到笔记
              </button>
            </div>
          </div>
        ))}
      </div>
    </>
  );
}

function NoteDisclosure({ note }: { note: Note }) {
  const previewRef = useRef<HTMLDivElement>(null);
  const detailCloseRef = useRef<HTMLButtonElement>(null);
  const [overflowing, setOverflowing] = useState(false);
  const [open, setOpen] = useState(false);
  useLayoutEffect(() => {
    const node = previewRef.current;
    if (!node) return;
    const measure = () => setOverflowing(node.scrollHeight > node.clientHeight + 1);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, [note.body]);
  useEffect(() => {
    if (!open) return;
    detailCloseRef.current?.focus();
    const close = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, [open]);
  return (
    <>
      <div
        ref={previewRef}
        className={`note-preview-clamp${overflowing ? " is-overflowing" : ""}`}
        aria-hidden="true"
      >
        <RichText text={note.body} />
      </div>
      {overflowing && (
        <button
          className="text-button note-more-button"
          aria-label={`查看完整笔记：${plainMarkdown(note.body).slice(0, 40)}`}
          onClick={() => setOpen(true)}
        >
          …显示更多
        </button>
      )}
      {open && (
        <div className="modal-backdrop" onMouseDown={(event) => {
          if (event.target === event.currentTarget) setOpen(false);
        }}>
          <section
            className="note-detail-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby={`note-detail-title-${note.note_id}`}
          >
            <header>
              <div>
                <span>投资笔记</span>
                <h2 id={`note-detail-title-${note.note_id}`}>{note.source_title || "笔记详情"}</h2>
              </div>
              <button ref={detailCloseRef} className="secondary" onClick={() => setOpen(false)}>
                关闭
              </button>
            </header>
            {note.quoted_text && <blockquote className="quoted-text">{note.quoted_text}</blockquote>}
            <div className="note-detail-content">
              <RichText text={note.body} />
            </div>
            <footer>
              <time>{new Date(note.updated_at ?? note.created_at).toLocaleString("zh-CN")}</time>
              {note.source_url && (
                <a href={note.source_url} target="_blank" rel="noreferrer" onClick={(event) => followSource(event, note.source_url!)}>
                  查看原始资料
                </a>
              )}
            </footer>
          </section>
        </div>
      )}
    </>
  );
}

function EditableNotes({
  notes,
  edit,
  remove,
  subjectLabel,
}: {
  notes: Note[];
  edit: (note: Note, body: string) => Promise<void>;
  remove: (note: Note) => void;
  subjectLabel?: (note: Note) => string;
}) {
  const [editing, setEditing] = useState<string | null>(null);
  const [body, setBody] = useState("");
  if (!notes.length) return <p className="empty">还没有笔记。</p>;
  return (
    <>
      <ul className="note-list">
        {notes.map((item) => (
          <li key={item.note_id}>
          {subjectLabel && (
            <span className="note-security">{subjectLabel(item)}</span>
          )}
          {item.source_title && item.source_url && (
            <a
              href={item.source_url}
              target="_blank"
              rel="noreferrer"
              onClick={(event) => followSource(event, item.source_url!)}
            >
              {item.source_title}
            </a>
          )}
          {editing === item.note_id ? (
            <div className="inline-editor">
              <textarea
                value={body}
                onChange={(event) => setBody(event.target.value)}
              />
              <div className="row-actions">
                <button
                  disabled={!body.trim()}
                  onClick={async () => {
                    await edit(item, body);
                    setEditing(null);
                  }}
                >
                  保存
                </button>
                <button className="secondary" onClick={() => setEditing(null)}>
                  取消
                </button>
              </div>
            </div>
          ) : (
            <>
              <NoteDisclosure note={item} />
              <div className="item-footer">
                <time>
                  {new Date(item.updated_at ?? item.created_at).toLocaleString(
                    "zh-CN",
                  )}
                </time>
                <div className="row-actions">
                  <button
                    className="text-button"
                    onClick={() => {
                      setEditing(item.note_id);
                      setBody(item.body);
                    }}
                  >
                    编辑
                  </button>
                  <button
                    className="text-button danger-text"
                    onClick={() => remove(item)}
                  >
                    删除
                  </button>
                </div>
              </div>
            </>
          )}
          </li>
        ))}
      </ul>
    </>
  );
}

function FinancialTable({ financials }: { financials: Financials | null }) {
  if (!financials?.periods.length)
    return <p className="empty">尚无已归档的结构化财务指标。</p>;
  return (
    <>
      <div className="table-wrap">
        <table className="data-table">
          <thead>
            <tr>
              <th>期间</th>
              <th className="numeric">ROE</th>
              <th className="numeric">毛利率</th>
              <th className="numeric">资产负债率</th>
              <th className="numeric">经营现金流</th>
              <th className="numeric">自由现金流</th>
            </tr>
          </thead>
          <tbody>
            {financials.periods.map((row) => (
              <tr key={row.period}>
                <td>{row.period_label}</td>
                <td className="numeric mono">{percent(row.roe)}</td>
                <td className="numeric mono">{percent(row.gross_margin)}</td>
                <td className="numeric mono">
                  {percent(row.debt_asset_ratio)}
                </td>
                <td className="numeric mono">
                  {amountYi(row.operating_cash_flow)}
                </td>
                <td className="numeric mono">{amountYi(row.free_cash_flow)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="hint">
        截止 {financials.cutoff_date} · {financials.source}
        <br />
        {financials.free_cash_flow_note}
      </p>
    </>
  );
}

function FundSecurity({
  data,
  selected,
  workspace,
  setSelected,
  editNote,
  deleteNote,
}: {
  data: Bootstrap;
  selected: Holding;
  workspace?: Workspace;
  setSelected: (id: string) => void;
  editNote: (note: Note, body: string) => Promise<void>;
  deleteNote: (note: Note) => void;
}) {
  const fund = workspace?.fund;
  const latest = fund?.nav_history[0];
  const events = fund?.nav_history.filter((item) => item.event) ?? [];
  return (
    <>
      <PageHeader
        eyebrow="基金资料"
        title={`${fund?.name ?? selected.name} ${selected.symbol}`}
        description="基金使用正式单位净值，不套用股票成交价、财报或公司公告模板。"
      />
      <div className="security-tabs" role="tablist">
        {data.holdings.map((item) => (
          <button
            className={
              item.security_id === selected.security_id
                ? "secondary selected"
                : "secondary"
            }
            key={item.security_id}
            onClick={() => setSelected(item.security_id)}
          >
            {item.symbol}
          </button>
        ))}
      </div>
      <div className="price-hero fund-nav-hero">
        <div>
          <span className="metric-label">最新单位净值</span>
          <div className="price-main mono">
            {latest?.nav?.toFixed(4) ?? "尚未归档"}
          </div>
          <div
            className={`price-change mono ${tone(latest?.change_percent ?? null)}`}
          >
            {percent(latest?.change_percent ?? null)} ·{" "}
            {latest?.date ?? "等待净值日期"}
          </div>
        </div>
        <div className="price-meta">
          <span>
            当前盈亏（估算）{" "}
            <b className={`mono ${tone(selected.estimated_profit_cny)}`}>
              {money(selected.estimated_profit_cny)}
            </b>
          </span>
          <span>买入金额 {invested(selected.invested_amount_cny)}</span>
          <span>截止 {fund?.cutoff_date ?? day(selected.trade_date)}</span>
        </div>
      </div>
      <div className="fund-layout">
        <Card title="近期净值" className="fund-nav-history">
          <div className="table-wrap">
            <table className="data-table compact-table">
              <thead>
                <tr>
                  <th>日期</th>
                  <th className="numeric">单位净值</th>
                  <th className="numeric">日涨跌</th>
                </tr>
              </thead>
              <tbody>
                {fund?.nav_history.map((row) => (
                  <tr key={row.date}>
                    <td className="mono">{row.date}</td>
                    <td className="numeric mono">{row.nav.toFixed(4)}</td>
                    <td className={`numeric mono ${tone(row.change_percent)}`}>
                      {percent(row.change_percent)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {!fund?.nav_history.length && (
            <p className="empty">尚无日期校验通过的基金净值。</p>
          )}
        </Card>
        <Card title="基金费用">
          <dl className="source-list">
            <div>
              <dt>管理费</dt>
              <dd>{fund?.fees.management_rate ?? "来源未披露"}</dd>
            </div>
            <div>
              <dt>托管费</dt>
              <dd>{fund?.fees.custodian_rate ?? "来源未披露"}</dd>
            </div>
            <div>
              <dt>销售服务费</dt>
              <dd>{fund?.fees.sales_service_rate ?? "来源未披露"}</dd>
            </div>
          </dl>
        </Card>
        <Card title="基金经理画像">
          {fund?.managers.length ? (
            <div className="manager-table-scroll" tabIndex={0} aria-label="基金经理画像，可横向滚动">
            <ul className="manager-list">
              {fund.managers.map((manager) => (
                <li key={manager.name}>
                  <strong>{manager.name}</strong>
                  <span>任职 {manager.work_time || "未披露"}</span>
                  <span>在管规模 {manager.managed_scale || "未披露"}</span>
                  <span>
                    任期回报{" "}
                    <b
                      className={`mono ${tone(manager.tenure_return_percent)}`}
                    >
                      {percent(manager.tenure_return_percent)}
                    </b>
                  </span>
                </li>
              ))}
            </ul>
            </div>
          ) : (
            <p className="empty">尚无可核验的基金经理资料。</p>
          )}
        </Card>
        <Card title="近期事件">
          {events.length ? (
            <ul className="event-stream">
              {events.map((row) => (
                <li key={row.date}>
                  <time>{row.date}</time>
                  <span>{row.event}</span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="empty">近期净值记录中没有分红或份额调整事件。</p>
          )}
        </Card>
        <Card title="基金笔记">
          <EditableNotes
            notes={workspace?.notes ?? []}
            edit={editNote}
            remove={deleteNote}
          />
        </Card>
      </div>
      <p className="hint">
        {fund?.source ?? "基金公开资料尚未归档"}
        。基金经理指标用于资料整理，不构成评价或建议。
      </p>
    </>
  );
}

function Security({
  data,
  selected,
  workspace,
  setSelected,
  saveExcerpt,
  editNote,
  deleteNote,
}: {
  data: Bootstrap;
  selected: Holding;
  workspace?: Workspace;
  setSelected: (id: string) => void;
  saveExcerpt: (item: Material, quoted: string, body: string) => Promise<void>;
  editNote: (note: Note, body: string) => Promise<void>;
  deleteNote: (note: Note) => void;
}) {
  const [tab, setTab] = useState("overview");
  const [material, setMaterial] = useState<Material | null>(null);
  const [quoted, setQuoted] = useState("");
  const [body, setBody] = useState("");
  const materials = workspace?.materials ?? [];
  const isHongKong = selected.asset_type === "hk_stock";
  const financialReports = materials.filter(
    (item) => item.material_type === "财务报告",
  );
  const openExcerpt = (item: Material) => {
    setMaterial(item);
    setQuoted(item.excerpt || item.title);
    setBody("");
  };
  if (selected.asset_type === "fund")
    return (
      <FundSecurity
        data={data}
        selected={selected}
        workspace={workspace}
        setSelected={setSelected}
        editNote={editNote}
        deleteNote={deleteNote}
      />
    );
  return (
    <>
      <div className="tabs" role="tablist">
        {[
          ["overview", "行情概览"],
          ["financials", isHongKong ? "财务披露" : "财务数据"],
          ["materials", "公告资料"],
          ["notes", "我的笔记"],
        ].map(([key, label]) => (
          <button
            key={key}
            className={`tab ${tab === key ? "active" : ""}`}
            onClick={() => setTab(key)}
          >
            {label}
          </button>
        ))}
      </div>
      {tab === "overview" && (
        <div className="security-overview-grid">
          <Card title={`本次行情 · ${selected.data_label}`}>
            <div className="security-overview-scroll" tabIndex={0} aria-label="行情概览指标，可横向滚动">
              <div className="security-overview-metrics">
              <div className="metric-card">
                <div className="label">买入金额</div>
                <div className="value">{invested(selected.invested_amount_cny)}</div>
              </div>
              <div className="metric-card">
                <div className="label">估算盈亏</div>
                <div className={`value ${tone(selected.estimated_profit_cny)}`}>
                  {selected.estimated_profit_cny === null ? "—" : money(selected.estimated_profit_cny)}
                </div>
              </div>
              <div className="metric-card">
                <div className="label">数据日期</div>
                <div className="value" style={{ fontSize: "18px" }}>{day(selected.trade_date)}</div>
              </div>
              <div className="metric-card">
                <div className="label">盈亏口径</div>
                <div className="value" style={{ fontSize: "16px" }}>
                  {selected.profit_basis ?? selected.profit_reason ?? "—"}
                </div>
              </div>
              </div>
            </div>
          </Card>
          <Card title="最近资料" action={<span className="meta">{materials.length} 条已归档</span>}>
            <MaterialList materials={materials.slice(0, 3)} onExcerpt={openExcerpt} />
          </Card>
        </div>
      )}
      {tab === "financials" &&
        (isHongKong ? (
          <div className="surface-grid">
            <Card title="港股披露概览">
              <dl className="source-list">
                <div>
                  <dt>最近披露</dt>
                  <dd className="mono">
                    {materials[0]?.published_at ?? "尚未归档"}
                  </dd>
                </div>
                <div>
                  <dt>业绩及财务报告</dt>
                  <dd>{financialReports.length} 条</dd>
                </div>
                <div>
                  <dt>已归档官方披露</dt>
                  <dd>{materials.length} 条</dd>
                </div>
                <div>
                  <dt>来源</dt>
                  <dd>香港交易所披露易</dd>
                </div>
              </dl>
              <p className="hint">
                以上为官方披露的日期与数量摘要，不将非结构化 PDF 推算成
                ROE、毛利率等数值。
              </p>
            </Card>
            <Card title="业绩与财务报告原文">
              <MaterialList
                materials={materials}
                type="财务报告"
                onExcerpt={openExcerpt}
              />
            </Card>
          </div>
        ) : (
          <div className="surface-grid">
            <Card title="历史财务数据">
              <FinancialTable financials={workspace?.financials ?? null} />
            </Card>
            <Card title="财务报告原文">
              <MaterialList
                materials={materials}
                type="财务报告"
                onExcerpt={openExcerpt}
              />
            </Card>
          </div>
        ))}
      {tab === "materials" && (
        <Card title={isHongKong ? "港交所公司公告" : "公司公告"}>
          <MaterialList
            materials={materials.filter(
              (item) => item.material_type !== "财务报告",
            )}
            onExcerpt={openExcerpt}
          />
        </Card>
      )}
      {tab === "notes" && (
        <Card title="证券笔记">
          <EditableNotes
            notes={workspace?.notes ?? []}
            edit={editNote}
            remove={deleteNote}
          />
        </Card>
      )}
      {material && (
        <div className="excerpt-panel" role="dialog" aria-modal="true">
          <div className="excerpt-head">
            <div>
              <span>摘录资料</span>
              <strong>{material.title}</strong>
              <a
                href={material.source_url}
                target="_blank"
                rel="noreferrer"
                onClick={(event) => followSource(event, material.source_url)}
              >
                查看原文
              </a>
            </div>
            <button className="text-button" onClick={() => setMaterial(null)}>
              关闭
            </button>
          </div>
          <p className="hint">
            保存后，原文标题和链接会随摘录一同保留在笔记中。
          </p>
          <label>
            摘录内容
            <textarea
              value={quoted}
              onChange={(event) => setQuoted(event.target.value)}
            />
          </label>
          <label>
            我的判断
            <textarea
              value={body}
              onChange={(event) => setBody(event.target.value)}
            />
          </label>
          <button
            disabled={!quoted.trim() || !body.trim()}
            onClick={async () => {
              await saveExcerpt(material, quoted, body);
              setMaterial(null);
            }}
          >
            保存到笔记
          </button>
        </div>
      )}
    </>
  );
}

function AIQuickNote({
  data,
  selected,
  setSelected,
  accepted,
}: {
  data: Bootstrap;
  selected?: Holding;
  setSelected: (id: string) => void;
  accepted: () => Promise<void>;
}) {
  const [status, setStatus] = useState<AIStatus | null>(null);
  const [raw, setRaw] = useState("");
  const [draft, setDraft] = useState<QuickNoteDraft | null>(null);
  const [body, setBody] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("正在检查本机 Codex…");
  const [loginPending, setLoginPending] = useState(false);
  const refreshStatus = async () => {
    const next = await api<AIStatus>("/api/ai/status");
    setStatus(next);
    setNotice(next.detail);
    if (next.authenticated) setLoginPending(false);
  };
  useEffect(() => {
    void refreshStatus().catch((error) => setNotice(error.message));
  }, []);
  useEffect(() => {
    if (!loginPending) return;
    const timer = window.setInterval(
      () => void refreshStatus().catch(() => undefined),
      2000,
    );
    return () => window.clearInterval(timer);
  }, [loginPending]);
  const perform = async (task: () => Promise<void>) => {
    setBusy(true);
    try {
      await task();
    } catch (error) {
      setNotice(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  };
  const login = () =>
    perform(async () => {
      const result = await api<{ authUrl: string }>("/api/ai/login/chatgpt", {
        method: "POST",
      });
      await openExternal(result.authUrl);
      setLoginPending(true);
      setNotice("请在浏览器完成 ChatGPT 登录，完成后本页会自动更新");
    });
  const generate = () =>
    perform(async () => {
      if (!selected) throw new Error("请先添加并选择一个持仓");
      const result = await api<QuickNoteDraft>("/api/ai/quick-notes", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          security_id: selected.security_id,
          raw_text: raw,
        }),
      });
      setDraft(result);
      setBody(quickNoteBody(result.draft));
      setNotice("AI 草稿已生成；核对并编辑后再保存");
    });
  const accept = () =>
    perform(async () => {
      if (!draft) return;
      await api(`/api/ai/quick-notes/${draft.draft_id}/accept`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ body }),
      });
      await accepted();
      setDraft(null);
      setRaw("");
      setBody("");
      setNotice("已确认并保存到投资笔记");
    });
  return (
    <>
      <PageHeader
        eyebrow="AI 速记"
        title="把原始观察整理成可核对草稿"
        description="Codex 只整理你输入的文字；不会读取 Vault、补行情、给出买卖建议或自动保存。"
      />
      <div className="ai-status" role="status">
        <span
          className={status?.authenticated ? "ai-state-ready" : "ai-state-idle"}
        >
          {status?.authenticated ? "● 已连接" : "○ 未连接"}
        </span>
        <span>{notice}</span>
        {status?.account?.email && (
          <span className="mono">{status.account.email}</span>
        )}
        {!status?.authenticated && (
          <button
            disabled={busy || !status?.available}
            onClick={() => void login()}
          >
            {loginPending ? "等待登录…" : "使用 ChatGPT 登录"}
          </button>
        )}
      </div>
      {!status?.available && (
        <p className="ai-warning">
          需要先在本机安装 Codex CLI。AI 未启用时，普通投资笔记仍可完整使用。
        </p>
      )}
      <div className="security-tabs" role="tablist">
        {data.holdings.map((item) => (
          <button
            className={
              item.security_id === selected?.security_id
                ? "secondary selected"
                : "secondary"
            }
            key={item.security_id}
            onClick={() => setSelected(item.security_id)}
          >
            {item.name}
          </button>
        ))}
      </div>
      <div className="quick-note-grid">
        <Card title="原始速记">
          <label className="quick-note-field">
            <span>关联标的</span>
            <strong>
              {selected ? `${selected.name} ${selected.symbol}` : "尚无持仓"}
            </strong>
          </label>
          <label className="quick-note-field">
            <span>原文（将原样保留）</span>
            <textarea
              value={raw}
              onChange={(event) => setRaw(event.target.value)}
              placeholder="例如：今天回调，但渠道价格没明显变化。先不补仓，等下一份财报确认…"
            />
          </label>
          <button
            disabled={
              busy || !status?.authenticated || !selected || !raw.trim()
            }
            onClick={() => void generate()}
          >
            {busy && !draft ? "正在整理…" : "生成 AI 草稿"}
          </button>
        </Card>
        <Card title="待确认草稿">
          {!draft ? (
            <p className="empty">
              生成后会在这里显示结构化草稿。AI 输出与用户原文分开保存。
            </p>
          ) : (
            <div className="quick-note-review">
              <div className="ai-draft-summary">
                <strong>{draft.draft.title}</strong>
                {draft.draft.tags.length > 0 && (
                  <span>{draft.draft.tags.join(" · ")}</span>
                )}
                <dl>
                  <div>
                    <dt>事实陈述</dt>
                    <dd>{draft.draft.facts.length} 条</dd>
                  </div>
                  <div>
                    <dt>个人判断</dt>
                    <dd>{draft.draft.user_judgements.length} 条</dd>
                  </div>
                  <div>
                    <dt>待验证</dt>
                    <dd>{draft.draft.open_questions.length} 条</dd>
                  </div>
                  <div>
                    <dt>计划</dt>
                    <dd>{draft.draft.planned_actions.length} 条</dd>
                  </div>
                </dl>
              </div>
              <label className="quick-note-field">
                <span>确认后的正式笔记（可编辑）</span>
                <textarea
                  className="quick-note-draft"
                  value={body}
                  onChange={(event) => setBody(event.target.value)}
                />
              </label>
              <p className="hint">
                点击确认后才会写入正式笔记；历史 AI 草稿不会覆盖你的原文。
              </p>
              <button
                disabled={busy || !body.trim()}
                onClick={() => void accept()}
              >
                {busy ? "正在保存…" : "确认并保存到投资笔记"}
              </button>
            </div>
          )}
        </Card>
      </div>
    </>
  );
}

function Research({
  data,
  workspaces,
  selected,
  setSelected,
  saveNote,
  editNote,
  deleteNote,
  reload,
}: {
  data: Bootstrap;
  workspaces: Record<string, Workspace>;
  selected?: Holding;
  setSelected: (id: string) => void;
  saveNote: (body: string, securityId?: string) => Promise<void>;
  editNote: (note: Note, body: string) => Promise<void>;
  deleteNote: (note: Note) => void;
  reload: () => Promise<void>;
}) {
  const [note, setNote] = useState("");
  const noteEditorRef = useRef<HTMLTextAreaElement>(null);
  const [status, setStatus] = useState<AIStatus | null>(null);
  const [draft, setDraft] = useState<QuickNoteDraft | null>(null);
  const [draftBody, setDraftBody] = useState("");
  const [working, setWorking] = useState(false);
  const [notice, setNotice] = useState("");
  const [scopeId, setScopeId] = useState(selected?.security_id ?? "all");
  const [query, setQuery] = useState("");
  const expandInlineSelection = (value: string, start: number, end: number) => {
    if (value.slice(start, start + 2) === "**" && value.slice(end - 2, end) === "**")
      return { start: start + 2, end: end - 2 };
    if (value[start] === "*" && value[end - 1] === "*")
      return { start: start + 1, end: end - 1 };
    return { start, end };
  };
  const applyNoteFormat = (
    format: "bold" | "italic" | "quote" | "unordered" | "ordered",
  ) => {
    const editor = noteEditorRef.current;
    if (!editor) return;
    const expanded = expandInlineSelection(note, editor.selectionStart, editor.selectionEnd);
    const start = expanded.start;
    const end = expanded.end;
    const selectedText = note.slice(start, end);
    let replacement = selectedText;
    let selectionStart = start;
    let selectionEnd = end;
    if (format === "bold" || format === "italic") {
      const marker = format === "bold" ? "**" : "*";
      const content = selectedText || (format === "bold" ? "加粗文字" : "斜体文字");
      replacement = `${marker}${content}${marker}`;
      selectionStart = start + marker.length;
      selectionEnd = selectionStart + content.length;
    } else {
      const lineStart = note.lastIndexOf("\n", Math.max(0, start - 1)) + 1;
      const nextBreak = note.indexOf("\n", end);
      const lineEnd = nextBreak === -1 ? note.length : nextBreak;
      const lines = note.slice(lineStart, lineEnd).split("\n");
      replacement = lines
        .map((line, index) => `${format === "quote" ? "> " : format === "unordered" ? "- " : `${index + 1}. `}${line}`)
        .join("\n");
      setNote(note.slice(0, lineStart) + replacement + note.slice(lineEnd));
      selectionStart = lineStart;
      selectionEnd = lineStart + replacement.length;
      requestAnimationFrame(() => {
        editor.focus();
        editor.setSelectionRange(selectionStart, selectionEnd);
      });
      return;
    }
    setNote(note.slice(0, start) + replacement + note.slice(end));
    requestAnimationFrame(() => {
      editor.focus();
      editor.setSelectionRange(selectionStart, selectionEnd);
    });
  };
  const refreshAI = async () => {
    const next = await api<AIStatus>("/api/ai/status");
    setStatus(next);
    setNotice(next.detail);
  };
  useEffect(() => {
    void refreshAI().catch((error) => setNotice(error.message));
  }, []);
  const marketWorkspace = workspaces[marketOverviewSubject.security_id];
  if (!selected)
    return (
      <>
        <PageHeader
          eyebrow="投资笔记"
          title="我的复盘记录"
          description="添加持仓后，可以为每只证券或基金保存复盘笔记。"
        />
        <p className="empty">当前没有持仓证券。</p>
        <Card title="大盘行情笔记">
          <EditableNotes
            notes={marketWorkspace?.notes ?? []}
            edit={editNote}
            remove={deleteNote}
          />
        </Card>
      </>
    );
  const editorSubject =
    scopeId === marketOverviewSubject.security_id
      ? marketOverviewSubject
      : data.holdings.find((item) => item.security_id === scopeId) ?? selected;
  const allNotes = Object.values(workspaces)
    .flatMap((item) => item.notes)
    .filter((item) => scopeId === "all" || item.security_id === scopeId)
    .filter((item) => item.body.toLowerCase().includes(query.trim().toLowerCase()))
    .sort((a, b) =>
      (b.updated_at ?? b.created_at).localeCompare(a.updated_at ?? a.created_at),
    );
  const organize = async () => {
    setWorking(true);
    try {
      const result = await api<QuickNoteDraft>("/api/ai/quick-notes", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          security_id: editorSubject.security_id,
          raw_text: note,
        }),
      });
      setDraft(result);
      setDraftBody(quickNoteBody(result.draft));
      setNotice("AI 草稿已生成；核对后再保存");
    } catch (error) {
      setNotice(error instanceof Error ? error.message : String(error));
    } finally {
      setWorking(false);
    }
  };
  const accept = async () => {
    if (!draft) return;
    setWorking(true);
    try {
      await api(`/api/ai/quick-notes/${draft.draft_id}/accept`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ body: draftBody }),
      });
      setNote("");
      setDraft(null);
      setDraftBody("");
      await reload();
      setNotice("AI 整理后的笔记已保存");
    } finally {
      setWorking(false);
    }
  };
  const connect = async () => {
    setWorking(true);
    try {
      const result = await api<{ authUrl: string }>("/api/ai/login/chatgpt", {
        method: "POST",
      });
      await openExternal(result.authUrl);
      setNotice("请在浏览器完成登录，然后点击刷新登录状态");
    } finally {
      setWorking(false);
    }
  };
  return (
    <>
      <PageHeader
        eyebrow="投资笔记"
        title="研究记录"
        description="记录事实、判断与待验证问题；所有笔记均可追溯到对应持仓或市场资料。"
      />
      <div className="research-workbench">
        <div className="sidebar-rail research-scope-pane">
          <div style={{ padding: "12px 14px", borderBottom: "1px solid var(--border)" }}>
            <input
              type="text"
              placeholder="筛选笔记…"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              style={{ width: "100%", padding: "7px 10px", border: "1px solid var(--border)", borderRadius: "var(--radius-sm)", fontSize: "13px" }}
            />
          </div>
          <div style={{ padding: "8px 0" }}>
            <div style={{ padding: "9px 14px", fontSize: "12px", fontWeight: 700, color: "var(--subtle)", textTransform: "uppercase", letterSpacing: "0.05em" }}>
              按持仓
            </div>
            <button
              className={`note-scope-option ${scopeId === "all" ? "active" : ""}`}
              onClick={() => { setScopeId("all"); setDraft(null); }}
            >
              全部笔记
            </button>
            {data.holdings.map((item) => (
              <button
                key={item.security_id}
                className={`note-scope-option ${scopeId === item.security_id ? "active" : ""}`}
                onClick={() => {
                  setScopeId(item.security_id);
                  setSelected(item.security_id);
                  setDraft(null);
                }}
              >
                {item.name}
              </button>
            ))}
            <button
              className={`note-scope-option ${scopeId === marketOverviewSubject.security_id ? "active" : ""}`}
              onClick={() => { setScopeId(marketOverviewSubject.security_id); setDraft(null); }}
            >
              市场概览
            </button>
          </div>
        </div>
        <Card title="最近笔记" action={<span className="meta">共 {allNotes.length} 条</span>} style={{}}>
          <EditableNotes
            notes={allNotes.slice(0, 20)}
            edit={editNote}
            remove={deleteNote}
            subjectLabel={(item) =>
              data.holdings.find(
                (holding) => holding.security_id === item.security_id,
              )?.name ?? "市场概览"
            }
          />
        </Card>
        <div className="research-editor-pane">
          <div className="editor-shell">
            <div className="editor-toolbar">
              <button type="button" aria-label="粗体" onClick={() => applyNoteFormat("bold")}>B</button>
              <button type="button" aria-label="斜体" onClick={() => applyNoteFormat("italic")}>I</button>
              <button type="button" aria-label="引用" onClick={() => applyNoteFormat("quote")}>“</button>
              <button type="button" aria-label="无序列表" onClick={() => applyNoteFormat("unordered")}>≡</button>
              <button type="button" aria-label="有序列表" onClick={() => applyNoteFormat("ordered")}>1.</button>
              <div style={{ flex: 1 }} />
              <span className="tag tag-neutral">关联：{editorSubject.name}</span>
            </div>
            <textarea
              ref={noteEditorRef}
              className="editor-textarea"
              value={note}
              onChange={(event) => setNote(event.target.value)}
              placeholder="记录事实、判断、问题或下一次复核事项…"
            />
            <div className="editor-footer">
              <span style={{ fontSize: "12px", color: "var(--subtle)" }}>
                {status?.authenticated ? "Codex 已连接" : "Codex 未连接"} · {notice || "未保存的更改将在保存后写入本地"}
              </span>
              <div style={{ display: "flex", gap: "8px" }}>
                <button
                  className="secondary"
                  disabled={!note.trim() || !status?.authenticated || working}
                  onClick={() => void organize()}
                >
                  {working ? "正在整理…" : "AI 速记"}
                </button>
                <button
                  disabled={!note.trim()}
                  onClick={async () => {
                    await saveNote(note, editorSubject.security_id);
                    setNote("");
                  }}
                >
                  保存笔记
                </button>
              </div>
            </div>
          </div>
          {draft && (
            <Card title="AI 待确认草稿">
              <label className="quick-note-field">
                <span>可编辑的正式笔记</span>
                <textarea
                  className="quick-note-draft"
                  value={draftBody}
                  onChange={(event) => setDraftBody(event.target.value)}
                />
              </label>
              <div className="button-row">
                <button
                  disabled={!draftBody.trim() || working}
                  onClick={() => void accept()}
                >
                  确认并保存
                </button>
                <button className="secondary" onClick={() => setDraft(null)}>
                  放弃草稿
                </button>
              </div>
            </Card>
          )}
        </div>
      </div>
    </>
  );
}

function ResearchAssistant({
  selected,
  saveNote,
  scene = "security",
  beforeMarketReport,
  initialMarketStage,
}: {
  selected?: Pick<Holding, "security_id" | "name" | "symbol">;
  saveNote?: (body: string) => Promise<void>;
  scene?: "security" | "market";
  beforeMarketReport?: () => Promise<MarketReportStage | undefined>;
  initialMarketStage?: MarketReportStage;
}) {
  const timelineRef = useRef<HTMLDivElement>(null);
  const requestToken = useRef(0);
  const [roles, setRoles] = useState<AIRole[]>([]);
  const [roleId, setRoleId] = useState("general");
  const [mode, setMode] = useState<ChatMode>("assistant");
  const [marketStage, setMarketStage] = useState<MarketReportStage | undefined>(initialMarketStage);
  const [marketStyle, setMarketStyle] = useState("dalio");
  const [threads, setThreads] = useState<ChatThread[]>([]);
  const [thread, setThread] = useState<ChatDetail | null>(null);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [savedEvent, setSavedEvent] = useState("");
  const loadThreads = async (openLatest = false) => {
    if (!selected) return;
    const list = await api<ChatThread[]>(
      `/api/ai/chats?security_id=${encodeURIComponent(selected.security_id)}`,
    );
    setThreads(list);
    if (openLatest && list[0]) {
      const latest = list[0];
      const detail = await api<ChatDetail>(`/api/ai/chats/${latest.thread_id}`);
      setThread(detail);
      setRoleId(detail.role_id === "coordinator" ? "general" : detail.role_id);
      setMode(detail.thread_type);
      if (scene === "market")
        setMarketStyle(detail.thread_type === "committee" ? "committee" : detail.role_id === "general" ? "dalio" : detail.role_id);
    }
  };
  useEffect(() => {
    void api<AIRole[]>("/api/ai/roles").then(setRoles);
  }, []);
  useEffect(() => {
    if (scene === "market" && initialMarketStage) setMarketStage(initialMarketStage);
  }, [scene, initialMarketStage?.label]);
  useEffect(() => {
    setThread(null);
    setSavedEvent("");
    void loadThreads(true);
  }, [selected?.security_id]);
  useEffect(() => {
    const timeline = timelineRef.current;
    if (timeline) timeline.scrollTop = timeline.scrollHeight;
  }, [thread?.events.length]);
  useEffect(() => {
    if (!thread || thread.active_run?.status !== "running" || busy) return;
    const threadId = thread.thread_id;
    const timer = window.setInterval(() => {
      void api<ChatDetail>(`/api/ai/chats/${threadId}`).then((detail) => {
        if (detail.thread_id === threadId) setThread(detail);
      });
    }, 800);
    return () => window.clearInterval(timer);
  }, [thread?.thread_id, thread?.active_run?.status, busy]);
  const clearThread = async () => {
    if (!thread) return;
    requestToken.current += 1;
    await api(`/api/ai/chats/${thread.thread_id}/archive`, { method: "POST" });
    setThread(null);
    setText("");
    setSavedEvent("");
    await loadThreads();
    setNotice("旧对话已清空；后续提问会创建独立对话");
  };
  const send = async (
    contentOverride?: string,
    modeOverride?: ChatMode,
    roleOverride?: string,
    forceNew = false,
  ) => {
    const content = (contentOverride ?? text).trim();
    const activeMode = modeOverride ?? mode;
    const activeRoleId = roleOverride ?? roleId;
    if (!selected || !content) return;
    setBusy(true);
    setNotice("");
    const token = ++requestToken.current;
    try {
      if (forceNew && thread) {
        await api(`/api/ai/chats/${thread.thread_id}/archive`, { method: "POST" });
      }
      let active = forceNew ? null : thread;
      if (!active) {
        const created = await api<ChatThread>("/api/ai/chats", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            security_id: selected.security_id,
            role_id: activeMode === "committee" ? "general" : activeRoleId,
            mode: activeMode,
            title: content.slice(0, 40),
          }),
        });
        active = { ...created, events: [] };
        setThread(active);
      }
      const result = await api<{ status?: string }>(
        `/api/ai/chats/${active.thread_id}/messages`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            content,
            role_id: activeMode === "committee" ? "general" : activeRoleId,
          }),
        },
      );
      if (scene !== "market") setText("");
      if (activeMode === "committee" && result.status === "running") {
        while (requestToken.current === token) {
          const detail = await api<ChatDetail>(
            `/api/ai/chats/${active.thread_id}`,
          );
          setThread(detail);
          if (detail.active_run?.status !== "running") {
            if (detail.active_run?.status === "failed")
              setNotice("本轮投委会未能完成，已保留当前研究进度。");
            break;
          }
          await new Promise((resolve) => window.setTimeout(resolve, 800));
        }
      } else {
        setThread(await api<ChatDetail>(`/api/ai/chats/${active.thread_id}`));
      }
      await loadThreads();
    } catch (error) {
      setNotice(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  };
  const generateMarketReport = async () => {
    const refreshedStage = beforeMarketReport ? await beforeMarketReport() : undefined;
    const stage = refreshedStage ?? marketStage;
    if (stage) setMarketStage(stage);
    await send(
      `生成${stage?.label ?? "当前阶段大盘行情报告"}，结合我的本地持仓给出条件化观察建议。`,
      marketStyle === "committee" ? "committee" : "assistant",
      marketStyle === "committee" ? "general" : marketStyle,
      true,
    );
  };
  if (!selected) return <p className="empty">添加持仓后可开始研究对话。</p>;
  const role = roles.find((item) => item.role_id === roleId);
  const excerpt = async (event: ChatEvent) => {
    if (!saveNote) return;
    const index =
      thread?.events.findIndex((item) => item.event_id === event.event_id) ??
      -1;
    const question =
      thread?.events
        .slice(0, index)
        .reverse()
        .find((item) => item.actor_type === "user")?.payload.content || "";
    const sources = event.payload.sources?.length
      ? `\n\n来源\n${event.payload.sources.map((item) => `- ${item.name}${item.as_of ? `（截至 ${item.as_of}）` : ""}${item.url ? ` ${item.url}` : ""}`).join("\n")}`
      : "";
    await saveNote(
      `${scene === "market" && marketStage ? `行情阶段\n${marketStage.label}\n\n` : ""}问题\n${question}\n\n${event.payload.role_name || "研究助手"}\n${cleanAssistantText(event.payload.content)}${sources}`,
    );
    setSavedEvent(event.event_id);
  };
  return (
    <section className="chat-main">
      <div className="assistant-titlebar">
        <div>
          <strong>
            {scene === "market"
              ? "专家风格行情报告"
              : mode === "committee"
                ? "AI 投资委员会"
                : "AI 研究助手"}
          </strong>
          <span>
            {scene === "market"
              ? `当前报告阶段：${marketStage?.label ?? "更新市场数据后自动识别"}；仅生成大盘行情报告并结合本地持仓给出条件化观察建议`
              : mode === "committee"
              ? "深度报告模式；简单问题请用普通助手"
              : "仅回答投资问题；每次提问独立分析，不自动带入旧对话"}
          </span>
        </div>
        {scene === "security" && <div className="button-row">
          {thread && (
            <button className="text-button" onClick={() => void clearThread()}>
              清空对话
            </button>
          )}
          <button
            className="text-button"
            onClick={() => {
              requestToken.current += 1;
              setThread(null);
              setText("");
            }}
          >
            新对话
          </button>
        </div>}
      </div>
      {scene === "security" && <div className="chat-mode-picker" role="tablist" aria-label="聊天模式">
        <button
          className={mode === "assistant" ? "selected" : "secondary"}
          onClick={() => {
            requestToken.current += 1;
            setMode("assistant");
            setThread(null);
            setText("");
          }}
        >
          普通助手
        </button>
        <button
          className={mode === "committee" ? "selected" : "secondary"}
          onClick={() => {
            requestToken.current += 1;
            setMode("committee");
            setThread(null);
            setText("");
          }}
        >
          投委会
        </button>
      </div>}
      {scene === "market" ? (
        <div className="market-report-controls">
          <label>
            <span>专家风格</span>
            <select value={marketStyle} onChange={(event) => setMarketStyle(event.target.value)}>
              <option value="committee">投委会风格</option>
              {roles.filter((item) => item.role_id !== "general").map((item) => (
                <option key={item.role_id} value={item.role_id}>{item.name}</option>
              ))}
            </select>
          </label>
          <button disabled={busy} onClick={() => void generateMarketReport()}>
            {busy ? "正在生成…" : "生成最新行情报告"}
          </button>
        </div>
      ) : (
        <div className={thread ? "role-picker compact" : "role-picker"}>
          {mode === "assistant" ? (
            <label>
              <span>投资专家</span>
              <select
                value={roleId}
                onChange={(event) => setRoleId(event.target.value)}
              >
                {roles.map((item) => (
                  <option key={item.role_id} value={item.role_id}>
                    {item.name}
                  </option>
                ))}
              </select>
            </label>
          ) : (
            <div className="committee-routing">
              <strong>协调员自动选角</strong>
              <span>
                按 stock-analysis 的问题匹配规则，在 15 位投资专家中选择 6 位互补委员。
              </span>
            </div>
          )}
          {scene === "security" && mode === "assistant" && role && <p>{role.focus}</p>}
        </div>
      )}
      <div className="chat-timeline" ref={timelineRef} aria-live="polite">
        {!thread?.events.length && (
          <p className="empty">
            {scene === "market"
              ? "生成当前市场行情报告。助手会区分盘前、盘中和盘后数据，并基于本地账本说明持仓暴露与下一步观察条件。"
              : mode === "committee"
              ? "研究计划、证据、专家意见和报告将在这里展开。"
              : `向 ${role?.name ?? "通用模式"} 提问。助手会按信源读取左侧标的的已归档证据、关联资料和历史笔记。`}
          </p>
        )}
        {thread?.active_run?.status === "running" && (
          <div className="committee-live-status" role="status">
            <span aria-hidden="true" />
            {(
              {
                planning: "协调员正在制定研究计划",
                evidence: "正在收集并核对研究证据",
                analysis: "投资专家正在并行分析",
                conflicts: "协调员正在整理共识与分歧",
                risk_review: "正在审查风险与组合影响",
                reporting: "正在生成最终深度报告",
              } as Record<string, string>
            )[thread.active_run.current_stage] || "投委会正在研究"}
          </div>
        )}
        {thread?.events.map((event) =>
          event.actor_type === "system" ? (
            <article
              className={`context-event ${event.event_type.endsWith(".started") ? "running" : ""}`}
              key={event.event_id}
            >
              <header>
                {event.event_type === "planning.started"
                  ? "协调员正在拆解问题"
                  : event.event_type === "evidence.started"
                    ? "证据研究员正在取证"
                    : event.event_type === "analysis.started"
                      ? "研究小组开始并行分析"
                      : event.event_type === "expert.started"
                        ? `${event.payload.role_name || "研究员"}正在分析`
                        : event.event_type === "reporting.started"
                          ? "报告编辑器正在生成深度报告"
                          : event.event_type === "workflow.failed"
                            ? "本轮投委会未完成"
                            : event.event_type === "routing.completed"
                              ? "协调员已完成问题分流"
                              : event.event_type === "plan.completed"
                                ? "协调员已制定研究计划"
                                : event.event_type === "conflicts.completed"
                                  ? "共识与分歧已整理"
                                  : event.event_type === "risk_review.completed"
                                    ? "风险与组合审查已完成"
                                    : event.event_type === "expert.failed"
                                      ? `${event.payload.role_name || "研究员"}未完成`
                                      : event.event_type.startsWith("tool.")
                                        ? `已补充${event.payload.skill_name || "研究资料"}`
                                        : "研究资料已更新"}
              </header>
              {Boolean(event.payload.assignments?.length) && (
                <p>
                  研究小组：
                  {event.payload
                    .assignments!.map(
                      (item) => `${item.name}（${item.function}）`,
                    )
                    .join("、")}
                </p>
              )}
              {!event.payload.assignments?.length &&
                Boolean(event.payload.selected_roles?.length) && (
                  <p>研究小组：{event.payload.selected_roles!.join("、")}</p>
                )}
              {event.payload.reason && <p>分派依据：{event.payload.reason}</p>}
              {Boolean(event.payload.gaps?.length) && (
                <small>仍待核实：{event.payload.gaps!.join("；")}</small>
              )}
              {event.payload.sources?.map((item, index) =>
                item.url ? (
                  <a
                    key={`${item.url}-${index}`}
                    href={item.url}
                    target="_blank"
                    rel="noreferrer"
                    onClick={(click) => followSource(click, item.url)}
                  >
                    {item.name}
                    {item.as_of ? ` · ${item.as_of}` : ""}
                  </a>
                ) : null,
              )}
              {event.payload.materials?.map((item) => (
                <a
                  key={item.source_url}
                  href={item.source_url}
                  target="_blank"
                  rel="noreferrer"
                  onClick={(click) => followSource(click, item.source_url)}
                >
                  {item.published_at} · {item.title} · {item.source_name}
                </a>
              ))}
            </article>
          ) : (
            <article
              className={`chat-message ${event.actor_type} ${event.event_type === "report.completed" ? "report" : ""}`}
              key={event.event_id}
            >
              <header>
                <span>
                  {event.actor_type === "user"
                    ? "你"
                    : event.payload.role_name || "研究助手"}
                </span>
                {event.actor_type === "assistant" && saveNote && (
                  <button
                    className="text-button"
                    onClick={() => void excerpt(event)}
                  >
                    {savedEvent === event.event_id ? "已摘录" : "摘录到笔记"}
                  </button>
                )}
              </header>
              <RichText text={event.payload.content} />
              {Boolean(event.payload.sources?.length) && (
                <div className="chat-sources">
                  <strong>参考来源</strong>
                  {event.payload.sources!.map((item, index) =>
                    item.url ? (
                      <a
                        key={`${item.url}-${index}`}
                        href={item.url}
                        target="_blank"
                        rel="noreferrer"
                        onClick={(click) => followSource(click, item.url)}
                      >
                        {item.name}
                        {item.as_of ? ` · ${item.as_of}` : ""}
                      </a>
                    ) : (
                      <span key={`${item.name}-${index}`}>
                        {item.name}
                        {item.as_of ? ` · ${item.as_of}` : ""}
                      </span>
                    ),
                  )}
                </div>
              )}
              {Boolean(event.payload.assumptions?.length) && (
                <small>
                  提议与推断：{event.payload.assumptions!.join("；")}
                </small>
              )}
              {Boolean(event.payload.unknowns?.length) && (
                <small>待补证据：{event.payload.unknowns!.join("；")}</small>
              )}
            </article>
          ),
        )}
      </div>
      {scene === "market" ? null : <div className="chat-composer">
        <label>
          <span>
            {mode === "committee"
                ? "输入深度复盘问题"
                : "输入投资问题"}
          </span>
          <textarea
            value={text}
            onChange={(event) => setText(event.target.value)}
            placeholder={
              mode === "committee"
                ? "例如：深度复盘这只基金的持仓结构、流动性压力、主要分歧和组合风险"
                : "例如：现金流变化是否削弱原投资逻辑？"
            }
            onKeyDown={(event) => {
              if ((event.metaKey || event.ctrlKey) && event.key === "Enter")
                void send();
            }}
          />
        </label>
        <button disabled={busy || !text.trim()} onClick={() => void send()}>
          {busy
            ? mode === "committee"
              ? "投委会研究中…"
              : "正在分析…"
            : "发送"}
        </button>
      </div>}
      {notice && <p className="ai-warning">{notice}</p>}
    </section>
  );
}

function SecurityWorkbench({
  data,
  selected,
  workspace,
  setSelected,
  saveExcerpt,
  editNote,
  deleteNote,
  saveNote,
  refreshData,
}: {
  data: Bootstrap;
  selected: Holding;
  workspace?: Workspace;
  setSelected: (id: string) => void;
  saveExcerpt: (item: Material, quoted: string, body: string) => Promise<void>;
  editNote: (note: Note, body: string) => Promise<void>;
  deleteNote: (note: Note) => void;
  saveNote: (body: string) => Promise<void>;
  refreshData: () => Promise<void>;
}) {
  const container = useRef<HTMLDivElement>(null);
  const dragging = useRef(false);
  const [leftPercent, setLeftPercent] = useState(() => {
    const saved = Number(localStorage.getItem("security-evidence-width"));
    return Number.isFinite(saved) && saved >= 38 && saved <= 72 ? saved : 57;
  });
  const resize = (clientX: number) => {
    if (!container.current) return;
    const bounds = container.current.getBoundingClientRect();
    setLeftPercent(
      Math.min(
        72,
        Math.max(38, ((clientX - bounds.left) / bounds.width) * 100),
      ),
    );
  };
  useEffect(() => {
    localStorage.setItem("security-evidence-width", String(leftPercent));
  }, [leftPercent]);
  useEffect(() => {
    const move = (event: PointerEvent) => {
      if (dragging.current) resize(event.clientX);
    };
    const stop = () => {
      dragging.current = false;
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", stop);
    return () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", stop);
    };
  }, []);
  return (
    <>
      <header className="security-quote-header">
        <div>
          <div style={{ fontSize: "12px", color: "var(--muted)", fontWeight: 600, marginBottom: "4px" }}>
            {assetLabel(selected.asset_type)} · {selected.symbol}
          </div>
          <h1 style={{ fontSize: "28px", fontWeight: 700, margin: 0, letterSpacing: "-0.02em" }}>
            {selected.name}
          </h1>
        </div>
        <div className="security-quote-price">
          <div className="mono security-price-value">
            {selected.price != null
              ? `¥${selected.price.toLocaleString("zh-CN", { maximumFractionDigits: 2 })}`
              : "—"}
          </div>
          <div className={`mono security-price-change ${selected.change_percent != null && selected.change_percent >= 0 ? "up" : "down"}`}>
            {selected.change_percent != null
              ? `${selected.change_percent >= 0 ? "+" : ""}${selected.change_percent.toFixed(2)}% · ${selected.data_session}${selected.trade_date ? ` · ${selected.trade_date.slice(0, 10)}` : ""}`
              : "等待行情数据"}
          </div>
        </div>
        <button className="secondary" onClick={() => void refreshData()}>刷新证券资料</button>
      </header>
      <p className="security-context-note">
        {selected.asset_type === "fund"
          ? "基金使用正式单位净值，不套用股票成交价、财报或公司公告模板。"
          : "行情用于定位资料时点；财务、公告、笔记与 AI 输出保持独立来源和口径。"}
      </p>
      <div className="security-tabs" role="tablist" aria-label="切换标的">
        {data.holdings.map((item) => (
          <button
            className={
              item.security_id === selected.security_id
                ? "secondary selected"
                : "secondary"
            }
            key={item.security_id}
            onClick={() => setSelected(item.security_id)}
          >
            {item.name} <span className="mono">{item.symbol}</span>
          </button>
        ))}
      </div>
      <div
        className="security-ai-workbench"
        ref={container}
        style={{ "--evidence-width": `${leftPercent}%` } as React.CSSProperties}
      >
        <section
          className="security-evidence-pane"
          aria-label={`${selected.name} 证券资料`}
        >
          <Security
            data={data}
            selected={selected}
            workspace={workspace}
            setSelected={setSelected}
            saveExcerpt={saveExcerpt}
            editNote={editNote}
            deleteNote={deleteNote}
          />
        </section>
        <div
          className="security-splitter"
          role="separator"
          aria-label="调整证券资料和助手宽度"
          aria-orientation="vertical"
          tabIndex={0}
          onPointerDown={(event) => {
            dragging.current = true;
            event.currentTarget.setPointerCapture(event.pointerId);
          }}
          onKeyDown={(event) => {
            if (event.key === "ArrowLeft")
              setLeftPercent((value) => Math.max(38, value - 2));
            if (event.key === "ArrowRight")
              setLeftPercent((value) => Math.min(72, value + 2));
          }}
        >
          <span />
        </div>
        <aside className="security-assistant-pane" aria-label="AI 研究助手">
          <ResearchAssistant selected={selected} saveNote={saveNote} />
        </aside>
      </div>
    </>
  );
}

function SettingsPage() {
  const [status, setStatus] = useState<AIStatus | null>(null);
  const [settings, setSettings] = useState<AISettings | null>(null);
  const [models, setModels] = useState<AIModel[]>([]);
  const [message, setMessage] = useState("正在读取 Codex 状态…");
  const [busy, setBusy] = useState(false);
  const load = async () => {
    const [nextStatus, nextSettings] = await Promise.all([
      api<AIStatus>("/api/ai/status"),
      api<AISettings>("/api/ai/settings"),
    ]);
    setStatus(nextStatus);
    setSettings(nextSettings);
    if (nextStatus.authenticated) {
      try {
        setModels(await api<AIModel[]>("/api/ai/models"));
      } catch (error) {
        setMessage(error instanceof Error ? error.message : "模型目录暂不可用");
        return;
      }
    }
    setMessage(nextStatus.detail);
  };
  useEffect(() => { void load(); }, []);
  const login = async () => {
    setBusy(true);
    try {
      const result = await api<{ authUrl: string }>("/api/ai/login/chatgpt", { method: "POST" });
      await openUrl(result.authUrl);
      setMessage("已打开 ChatGPT 登录页；完成后点击“刷新登录状态”。");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "无法启动登录");
    } finally { setBusy(false); }
  };
  const logout = async () => {
    setBusy(true);
    try {
      await api("/api/ai/logout", { method: "POST" });
      await load();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "退出登录失败");
    } finally { setBusy(false); }
  };
  const saveTask = async (
    task: AIModelTask,
    field: "model_id" | "reasoning_effort",
    value: string,
  ) => {
    if (!settings) return;
    const next = { ...settings.tasks[task], [field]: value || null };
    setSettings({ ...settings, tasks: { ...settings.tasks, [task]: next } });
    try {
      await api(`/api/ai/settings/models/${task}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(next),
      });
      setMessage("模型设置已保存，下一次生成开始生效。");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "模型设置保存失败");
      await load();
    }
  };
  const taskLabels: Array<[AIModelTask, string, string]> = [
    ["quick_note", "AI 速记", "整理原始投资笔记"],
    ["research", "研究助手", "单专家问答与市场报告"],
    ["committee", "投委会", "六人审议与深度报告"],
  ];
  return <>
    <PageHeader eyebrow="设置" title="AI 与模型" description="使用 Codex 管理 ChatGPT 登录；Invest Vault 不读取或保存访问令牌。" />
    <div className="settings-grid">
      <section className="card settings-account" aria-labelledby="codex-account-title">
        <div className="card-header">
          <div><p className="page-eyebrow">连接</p><h2 id="codex-account-title">Codex 登录状态</h2></div>
          <span className={`connection-state ${status?.authenticated ? "is-online" : ""}`}>
            {status?.authenticated ? "已连接" : "未连接"}
          </span>
        </div>
        <dl className="source-list">
          <div><dt>账号</dt><dd>{status?.account?.email || "尚未登录 ChatGPT"}</dd></div>
          <div><dt>方案</dt><dd>{status?.account?.planType || "—"}</dd></div>
          <div><dt>接入方式</dt><dd>本机 Codex app-server</dd></div>
        </dl>
        <p className="hint" role="status">{message}</p>
        <div className="settings-actions">
          {!status?.authenticated && <button disabled={busy || status?.available === false} onClick={() => void login()}>使用 ChatGPT 登录</button>}
          <button className="secondary" disabled={busy} onClick={() => void load()}>刷新登录状态</button>
          {status?.authenticated && <button className="secondary" disabled={busy} onClick={() => void logout()}>退出登录</button>}
        </div>
      </section>
      <section className="card settings-models" aria-labelledby="model-settings-title">
        <div className="card-header"><div><p className="page-eyebrow">按任务配置</p><h2 id="model-settings-title">模型与推理强度</h2></div></div>
        <p className="hint">留空时跟随 Codex 默认值。设置只影响下一次生成，不改变历史报告。</p>
        <div className="model-task-list">
          {taskLabels.map(([task, label, description]) => {
            const current = settings?.tasks[task];
            const selectedModel = models.find((model) => model.id === current?.model_id);
            const efforts = selectedModel?.supportedReasoningEfforts?.map((item) =>
              typeof item === "string" ? item : item.reasoningEffort
            ) || ["low", "medium", "high", "xhigh"];
            return <div className="model-task-row" key={task}>
              <div><strong>{label}</strong><span>{description}</span></div>
              <label><span>模型</span><select value={current?.model_id || ""} disabled={!status?.authenticated} onChange={(event) => void saveTask(task, "model_id", event.target.value)}>
                <option value="">Codex 默认模型</option>
                {models.map((model) => <option key={model.id} value={model.id}>{model.displayName}{model.isDefault ? "（默认）" : ""}</option>)}
              </select></label>
              <label><span>推理强度</span><select value={current?.reasoning_effort || ""} disabled={!status?.authenticated} onChange={(event) => void saveTask(task, "reasoning_effort", event.target.value)}>
                <option value="">跟随模型默认值</option>
                {efforts.map((effort) => <option key={effort} value={effort}>{effort}</option>)}
              </select></label>
            </div>;
          })}
        </div>
      </section>
    </div>
  </>;
}

function DataPage({
  data,
  download,
}: {
  data: Bootstrap;
  download: (url: string, kind: "markdown" | "backup") => void;
}) {
  return (
    <>
      <PageHeader
        eyebrow="数据与备份"
        title="本地数据管理"
        description="进入应用和重新回到前台时刷新最新行情；完整交易日数据继续独立归档。"
      />
      <ArchiveBanner data={data} />
      <div className="data-management-grid">
        <Card title="自动刷新与归档规则">
          <dl className="source-list">
            <div>
              <dt>盘前与盘中</dt>
              <dd>自动获取最新公开行情，并明确标注盘前或盘中实时数据</dd>
            </div>
            <div>
              <dt>盘后与非交易日</dt>
              <dd>显示最近完整交易日收盘数据；市场概览仍可手动重试</dd>
            </div>
            <div>
              <dt>失败处理</dt>
              <dd>保留上一次成功归档，不补零</dd>
            </div>
            <div>
              <dt>联网范围</dt>
              <dd>持仓证券、基金资料及市场概览栏目</dd>
            </div>
          </dl>
        </Card>
        <div className="data-export-stack">
          <Card title="研究笔记">
            <p>导出包含当前复盘笔记、资料摘录和数据截止时间。</p>
            <button onClick={() => download("/api/exports/research.md", "markdown")}>
              导出 Markdown
            </button>
          </Card>
          <Card title="完整备份">
            <p>包含本地数据库、盘后快照、资料、附件和校验和。</p>
            <button onClick={() => download("/api/exports/backup.zip", "backup")}>
              创建完整备份
            </button>
          </Card>
        </div>
      </div>
    </>
  );
}

function ConfirmDelete({
  request,
  busy,
  cancel,
  confirmDelete,
}: {
  request: DeleteRequest;
  busy: boolean;
  cancel: () => void;
  confirmDelete: () => void;
}) {
  const cancelRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    cancelRef.current?.focus();
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) cancel();
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [busy, cancel]);
  return (
    <div className="modal-backdrop">
      <section
        className="confirm-dialog"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="confirm-delete-title"
      >
        <span className="confirm-kicker">删除确认</span>
        <h2 id="confirm-delete-title">
          确认删除{request.kind === "holding" ? "这笔持仓" : "这条笔记"}？
        </h2>
        <p>{plainMarkdown(request.label)}</p>
        <p className="hint">此操作会永久删除本机中的对应记录，无法恢复。</p>
        <div className="confirm-actions">
          <button
            ref={cancelRef}
            className="secondary"
            disabled={busy}
            onClick={cancel}
          >
            取消
          </button>
          <button
            className="danger-button"
            disabled={busy}
            onClick={confirmDelete}
          >
            {busy ? "正在删除…" : "确认删除"}
          </button>
        </div>
      </section>
    </div>
  );
}

export function App() {
  const [active, setActive] = useState("today");
  const [data, setData] = useState<Bootstrap | null>(null);
  const [workspaces, setWorkspaces] = useState<Record<string, Workspace>>({});
  const [selectedId, setSelectedId] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("正在读取本地投资资料…");
  const [editorOpen, setEditorOpen] = useState(false);
  const [editingEntry, setEditingEntry] = useState<HoldingEntry | null>(null);
  const [deleteRequest, setDeleteRequest] = useState<DeleteRequest | null>(
    null,
  );
  const load = async (refresh = true) => {
    const next = await api<Bootstrap>(`/api/bootstrap?refresh=${refresh}`);
    setData(next);
    setSelectedId((current) =>
      next.holdings.some((item) => item.security_id === current)
        ? current
        : next.holdings[0]?.security_id || "",
    );
    const pairs = await Promise.all(
      [...next.holdings, marketOverviewSubject].map(
        async (item) =>
          [
            item.security_id,
            await api<Workspace>(`/api/research/${item.security_id}`),
          ] as const,
      ),
    );
    setWorkspaces(Object.fromEntries(pairs));
    setMessage(
      next.holdings.length
        ? `已归档 ${day(next.report_as_of)}（${next.archive_coverage.current}/${next.archive_coverage.total} 个标的已更新）`
        : "本地资料库为空，可添加持仓开始使用",
    );
  };
  const refreshData = async () => {
    await run(
      async () => {
        await api("/api/holdings/refresh", { method: "POST" });
        await load(false);
      },
      "正在刷新持仓行情并重算盈亏…",
      "持仓行情、证券资料与盈亏估算已更新",
    );
  };
  useEffect(() => {
    load().catch((error) => setMessage(`本地服务不可用：${error.message}`));
    let lastCheck = Date.now();
    const automaticCheck = () => {
      if (
        document.visibilityState !== "visible" ||
        Date.now() - lastCheck < 60_000
      )
        return;
      lastCheck = Date.now();
      load().catch((error) =>
        setMessage(`自动行情刷新未完成：${error.message}`),
      );
    };
    window.addEventListener("focus", automaticCheck);
    document.addEventListener("visibilitychange", automaticCheck);
    const now = new Date();
    const next = new Date(now);
    next.setHours(17, 31, 0, 0);
    if (next <= now) next.setDate(next.getDate() + 1);
    let dailyTimer: number | undefined;
    const firstTimer = window.setTimeout(() => {
      load().catch((error) =>
        setMessage(`自动行情刷新未完成：${error.message}`),
      );
      dailyTimer = window.setInterval(
        () =>
          load().catch((error) =>
            setMessage(`自动行情刷新未完成：${error.message}`),
          ),
        24 * 60 * 60 * 1000,
      );
    }, next.getTime() - now.getTime());
    return () => {
      window.removeEventListener("focus", automaticCheck);
      document.removeEventListener("visibilitychange", automaticCheck);
      window.clearTimeout(firstTimer);
      if (dailyTimer) window.clearInterval(dailyTimer);
    };
  }, []);
  const run = async (
    task: () => Promise<void>,
    pending: string,
    done: string,
  ) => {
    setBusy(true);
    setMessage(pending);
    try {
      await task();
      setMessage(done);
    } catch (error) {
      setMessage(
        `操作失败：${error instanceof Error ? error.message : String(error)}`,
      );
    } finally {
      setBusy(false);
    }
  };
  const selected =
    data?.holdings.find((item) => item.security_id === selectedId) ??
    data?.holdings[0];
  const saveHoldings = (rows: HoldingDraft[]) =>
    run(
      async () => {
        await api("/api/holdings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ rows }),
        });
        await load();
        setEditorOpen(false);
      },
      "正在保存持仓…",
      "持仓已保存",
    );
  const saveHoldingEdit = (rows: HoldingDraft[]) =>
    run(
      async () => {
        if (!editingEntry) return;
        await api(
          `/api/holdings/${encodeURIComponent(editingEntry.holding_id)}`,
          {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(rows[0]),
          },
        );
        await load();
        setEditingEntry(null);
      },
      "正在保存修订…",
      "持仓已修订",
    );
  const deleteHolding = (entry: HoldingEntry) =>
    setDeleteRequest({
      kind: "holding",
      entry,
      label: `${symbolOf(entry.security_id)} · 买入金额 ${invested(entry.invested_amount_cny)} · ${entry.bought_on}`,
    });
  const saveNote = (body: string, securityId?: string) =>
    run(
      async () => {
        const targetSecurityId = securityId ?? selected?.security_id;
        if (!targetSecurityId) throw new Error("没有可关联的证券");
        await api("/api/research/notes", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ security_id: targetSecurityId, body }),
        });
        await load();
      },
      "正在保存笔记…",
      "笔记已保存",
    );
  const editNote = (note: Note, body: string) =>
    run(
      async () => {
        await api(`/api/research/notes/${note.note_id}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ security_id: note.security_id, body }),
        });
        await load();
      },
      "正在修订笔记…",
      "笔记已修订",
    );
  const deleteNote = (note: Note) =>
    setDeleteRequest({
      kind: "note",
      note,
      label: note.body.length > 90 ? `${note.body.slice(0, 90)}…` : note.body,
    });
  const confirmDelete = () => {
    if (!deleteRequest) return;
    void run(
      async () => {
        if (deleteRequest.kind === "holding")
          await api(
            `/api/holdings/${encodeURIComponent(deleteRequest.entry.holding_id)}`,
            { method: "DELETE" },
          );
        else
          await api(`/api/research/notes/${deleteRequest.note.note_id}`, {
            method: "DELETE",
          });
        setDeleteRequest(null);
        await load();
      },
      deleteRequest.kind === "holding" ? "正在删除持仓…" : "正在删除笔记…",
      deleteRequest.kind === "holding" ? "持仓已删除" : "笔记已删除",
    );
  };
  const saveExcerpt = (item: Material, quoted_text: string, body: string) =>
    run(
      async () => {
        await api("/api/research/notes/from-material", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            security_id: item.security_id,
            material_id: item.material_id,
            quoted_text,
            body,
          }),
        });
        await load();
      },
      "正在保存资料摘录…",
      "资料已摘录到笔记",
    );
  const saveExport = async (
    url: string,
    kind: "xlsx" | "markdown" | "backup",
    suggestedName: string,
  ) => {
    const response = await fetch(url);
    if (!response.ok) throw new Error("导出文件生成失败");
    const raw = new Uint8Array(await response.arrayBuffer());
    const bytes = Array.from(raw);
    if (
      (window as unknown as { __TAURI_INTERNALS__?: object })
        .__TAURI_INTERNALS__
    )
      return invoke<string | null>("save_export", {
        bytes,
        suggestedName,
        kind,
      });
    const extension =
      kind === "markdown" ? "md" : kind === "backup" ? "zip" : "xlsx";
    const anchor = document.createElement("a");
    anchor.href = URL.createObjectURL(new Blob([raw]));
    anchor.download = `${suggestedName}.${extension}`;
    anchor.click();
    URL.revokeObjectURL(anchor.href);
    return suggestedName;
  };
  const exportExcel = () =>
    run(
      async () => {
        await saveExport(
          "/api/holdings/export.xlsx",
          "xlsx",
          `我的持仓-${new Date().toISOString().slice(0, 10)}`,
        );
      },
      "正在生成 Excel…",
      "如已选择位置，Excel 已保存",
    );
  const saveRiskProfile = (cashBalance: string, maxDrawdown: string) =>
    run(
      async () => {
        await api("/api/portfolio/risk-profile", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            cash_balance_cny: cashBalance,
            max_drawdown_percent: maxDrawdown,
          }),
        });
        await load();
      },
      "正在保存现金与风险约束…",
      "现金与风险约束已保存",
    );
  const download = (url: string, kind: "markdown" | "backup") =>
    void run(
      async () => {
        await saveExport(
          url,
          kind,
          `${kind === "markdown" ? "投资笔记" : "投资札记完整备份"}-${new Date().toISOString().slice(0, 10)}`,
        );
      },
      "正在生成导出文件…",
      "如已选择位置，文件已保存",
    );
  const addHoldings = () => {
    setEditingEntry(null);
    setEditorOpen(true);
  };
  const openHolding = (id: string) => {
    setSelectedId(id);
    setActive("security");
  };
  let content: React.ReactNode = <div className="loading-panel">{message}</div>;
  if (data) {
    if (editingEntry) {
      const summary = data.holdings.find(
        (item) => item.security_id === editingEntry.security_id,
      );
      content = (
        <HoldingEditor
          editing
          initialRows={[
            {
              row_id: editingEntry.holding_id,
              symbol: summary?.symbol ?? symbolOf(editingEntry.security_id),
              asset_type: editingEntry.asset_type,
              invested_amount_cny: editingEntry.invested_amount_cny,
              bought_on: editingEntry.bought_on,
            },
          ]}
          save={saveHoldingEdit}
          cancel={() => setEditingEntry(null)}
        />
      );
    } else if (editorOpen)
      content = (
        <HoldingEditor
          save={saveHoldings}
          cancel={() => setEditorOpen(false)}
        />
      );
    else if (active === "today")
      content = (
        <Today
          data={data}
          workspaces={workspaces}
          addHoldings={addHoldings}
          navigate={setActive}
          openHolding={openHolding}
        />
      );
    else if (active === "market")
      content = (
        <MarketPage
          market={data.market}
          reload={load}
          saveMarketNote={(body) => saveNote(body, marketOverviewSubject.security_id)}
        />
      );
    else if (active === "portfolio")
      content = (
        <Portfolio
          data={data}
          addHoldings={addHoldings}
          editHolding={setEditingEntry}
          deleteHolding={deleteHolding}
          exportExcel={() => void exportExcel()}
          saveRiskProfile={saveRiskProfile}
          refreshData={refreshData}
        />
      );
    else if (active === "security")
      content = selected ? (
        <SecurityWorkbench
          data={data}
          selected={selected}
          workspace={workspaces[selected.security_id]}
          setSelected={setSelectedId}
          saveExcerpt={saveExcerpt}
          editNote={editNote}
          deleteNote={deleteNote}
          saveNote={saveNote}
          refreshData={refreshData}
        />
      ) : (
        <>
          <PageHeader
            eyebrow="证券资料"
            title="持仓公司资料"
            description="添加持仓后归档财务、公告和笔记。"
          />
          <EmptyVault addHoldings={addHoldings} />
        </>
      );
    else if (active === "research")
      content = (
        <Research
          data={data}
          workspaces={workspaces}
          selected={selected}
          setSelected={setSelectedId}
          saveNote={saveNote}
          editNote={editNote}
          deleteNote={deleteNote}
          reload={load}
        />
      );
    else if (active === "data") content = <DataPage data={data} download={download} />;
    else content = <SettingsPage />;
  }
  const topbar =
    active === "today"
      ? ["今日复盘", "持仓行情与投资记录"]
      : active === "market"
        ? ["市场概览", "全球主要市场"]
        : active === "portfolio"
          ? ["持仓账本", "我的持仓"]
          : active === "security"
            ? [
                selected?.asset_type === "fund" ? "基金资料" : "证券资料",
                selected ? `${selected.name} · ${selected.symbol}` : "未选择",
              ]
            : active === "research"
              ? ["投资笔记", "研究记录"]
              : active === "data"
                ? ["数据与备份", "导出与恢复"]
                : ["设置", "AI 与模型"];
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [paletteQuery, setPaletteQuery] = useState("");
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setPaletteOpen((current) => !current);
      }
      if (event.key === "Escape") setPaletteOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  const paletteItems = useMemo(() => {
    const holdingBySecurity = new Map(
      (data?.holdings ?? []).map((item) => [item.security_id, item]),
    );
    const holdingsItems = (data?.holdings ?? []).map((item) => ({
      kind: "holding" as const,
      id: item.security_id,
      title: `${item.name} · ${item.symbol}`,
      subtitle: `${assetLabel(item.asset_type)} · 买入 ${invested(item.invested_amount_cny)}`,
      searchText: `${item.name} ${item.symbol} ${item.security_id} ${assetLabel(item.asset_type)}`,
      securityIdentifiers: [item.symbol, securityCode(item.security_id)],
      action: () => openHolding(item.security_id),
    }));
    const noteItems = Object.values(workspaces)
      .flatMap((ws) => ws.notes)
      .map((note) => {
        const holding = holdingBySecurity.get(note.security_id);
        const subjectName = holding?.name ?? "市场概览";
        const previewText = plainMarkdown(note.body);
        return {
          kind: "note" as const,
          id: note.note_id,
          title: previewText.slice(0, 60) + (previewText.length > 60 ? "…" : ""),
          subtitle: `${subjectName}${holding?.symbol ? ` · ${holding.symbol}` : ""} · ${new Date(note.updated_at ?? note.created_at).toLocaleDateString("zh-CN")}`,
          searchText: `${note.body} ${note.source_title ?? ""} ${note.quoted_text ?? ""} ${subjectName} ${holding?.symbol ?? ""} ${note.security_id}`,
          securityIdentifiers: [holding?.symbol ?? securityCode(note.security_id)],
          action: () => {
            setSelectedId(note.security_id);
            setActive("research");
          },
        };
      });
    const materialItems = Object.values(workspaces)
      .flatMap((ws) => ws.materials)
      .map((material) => {
        const holding = holdingBySecurity.get(material.security_id);
        return {
          kind: "material" as const,
          id: material.material_id,
          title: material.title,
          subtitle: `${holding?.name ?? material.security_id}${holding?.symbol ? ` · ${holding.symbol}` : ""} · ${material.source_name}`,
          searchText: `${material.title} ${material.excerpt} ${material.source_name} ${holding?.name ?? ""} ${holding?.symbol ?? ""} ${material.security_id}`,
          securityIdentifiers: [holding?.symbol ?? securityCode(material.security_id)],
          action: () => openHolding(material.security_id),
        };
      });
    const actions = [
      { kind: "action" as const, id: "refresh", title: "刷新全部市场数据", searchText: "刷新全部市场数据 行情", securityIdentifiers: [], shortcut: "⌘R", action: () => { if (active === "market") { /* market refreshes itself */ } else { setActive("market"); } } },
      { kind: "action" as const, id: "add", title: "添加新持仓", searchText: "添加新持仓 证券 基金", securityIdentifiers: [], shortcut: "⌘N", action: () => addHoldings() },
    ];
    const query = paletteQuery.trim();
    const filter = <T extends { title: string; subtitle?: string; searchText?: string; securityIdentifiers?: string[] },>(items: T[]): T[] => {
      if (!query) return items;
      if (isSecurityCodeQuery(query))
        return items.filter((item) => item.securityIdentifiers?.some(
          (identifier) => normalizeSearch(identifier) === normalizeSearch(query),
        ));
      return items.filter((item) => fuzzyMatch(`${item.title} ${item.subtitle ?? ""} ${item.searchText ?? ""}`, query));
    };
    return {
      holdings: filter(holdingsItems).slice(0, 5),
      notes: filter(noteItems).slice(0, 12),
      materials: filter(materialItems).slice(0, 12),
      actions: filter(actions),
    };
  }, [data, workspaces, paletteQuery]);
  const navigate = (key: string) => {
    setActive(key);
    setEditorOpen(false);
    setEditingEntry(null);
    setMessage(
      data?.holdings.length
        ? `已归档 ${day(data.report_as_of)}（${data.archive_coverage.current}/${data.archive_coverage.total} 个标的已更新）`
        : "本地资料库为空，可添加持仓开始使用",
    );
  };
  const navShortcuts = ["⌘1", "⌘2", "⌘3", "⌘4", "⌘5", "⌘6", "⌘7"];
  return (
    <div className={`app-shell ${active === "security" ? "security-layout" : ""}`}>
      <a className="skip-link" href="#content">
        跳转到主要内容
      </a>
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">IV</span>
          <span>投资札记</span>
        </div>
        <div className="nav-section-label">工作台</div>
        <nav aria-label="主要导航">
          {navItems.map((item, index) => (
            <button
              key={item.key}
              className={active === item.key ? "nav-active" : ""}
              onClick={() => navigate(item.key)}
            >
              {item.label}
              <span className="shortcut">{navShortcuts[index]}</span>
            </button>
          ))}
        </nav>
        <div className="side-foot">
          <span className="local-dot">● 本地优先 · 数据私有</span>
          <span>v0.3.24</span>
        </div>
      </aside>
      <main id="content">
        <div className="topbar">
          <div className="topbar-crumb">
            <span>{topbar[0]}</span>
            <span className="topbar-divider">/</span>
            <span>{topbar[1]}</span>
          </div>
          <div className="top-actions">
            <button
              className="search-trigger"
              onClick={() => setPaletteOpen(true)}
              aria-label="打开命令面板"
            >
              <span>⌘</span>
              <span>搜索持仓、笔记、资料…</span>
              <kbd>⌘K</kbd>
            </button>
            {active === "today" && (
              <button className="btn-sm" onClick={() => void load()}>
                刷新数据
              </button>
            )}
          </div>
        </div>
        <div className="status-line" role="status">
          {busy ? "处理中｜" : ""}
          {message}
        </div>
        {content}
      </main>
      {deleteRequest && (
        <ConfirmDelete
          request={deleteRequest}
          busy={busy}
          cancel={() => setDeleteRequest(null)}
          confirmDelete={confirmDelete}
        />
      )}
      {paletteOpen && data && (
        <div
          className="command-palette"
          onClick={(event) => {
            if (event.target === event.currentTarget) setPaletteOpen(false);
          }}
        >
          <div className="palette-dialog">
            <div className="palette-input">
              <span>⌘</span>
              <input
                type="text"
                value={paletteQuery}
                onChange={(event) => setPaletteQuery(event.target.value)}
                placeholder="搜索持仓、笔记、资料或执行操作…"
                autoFocus
              />
              <kbd>ESC</kbd>
            </div>
            <div className="palette-results">
            {paletteItems.holdings.length > 0 && (
              <div className="palette-section">
                <h4>持仓</h4>
                {paletteItems.holdings.map((item) => (
                  <div
                    key={item.id}
                    className="palette-item"
                    onClick={() => {
                      item.action();
                      setPaletteOpen(false);
                    }}
                  >
                    <div className="icon">◐</div>
                    <div className="content">
                      <div className="title">{item.title}</div>
                      <div className="subtitle">{item.subtitle}</div>
                    </div>
                    <kbd>↵</kbd>
                  </div>
                ))}
              </div>
            )}
            {paletteItems.notes.length > 0 && (
              <div className="palette-section">
                <h4>笔记</h4>
                {paletteItems.notes.map((item) => (
                  <div
                    key={item.id}
                    className="palette-item"
                    onClick={() => {
                      item.action();
                      setPaletteOpen(false);
                    }}
                  >
                    <div className="icon">✎</div>
                    <div className="content">
                      <div className="title">{item.title}</div>
                      <div className="subtitle">{item.subtitle}</div>
                    </div>
                    <kbd>↵</kbd>
                  </div>
                ))}
              </div>
            )}
            {paletteItems.materials.length > 0 && (
              <div className="palette-section">
                <h4>证券资料</h4>
                {paletteItems.materials.map((item) => (
                  <div
                    key={`${item.id}-${item.subtitle}`}
                    className="palette-item"
                    onClick={() => {
                      item.action();
                      setPaletteOpen(false);
                    }}
                  >
                    <div className="icon">▤</div>
                    <div className="content">
                      <div className="title">{item.title}</div>
                      <div className="subtitle">{item.subtitle}</div>
                    </div>
                    <kbd>↵</kbd>
                  </div>
                ))}
              </div>
            )}
            {paletteItems.actions.length > 0 && (
              <div className="palette-section">
                <h4>操作</h4>
                {paletteItems.actions.map((item) => (
                  <div
                    key={item.id}
                    className="palette-item"
                    onClick={() => {
                      item.action();
                      setPaletteOpen(false);
                    }}
                  >
                    <div className="icon">⟳</div>
                    <div className="content">
                      <div className="title">{item.title}</div>
                    </div>
                    <kbd>{item.shortcut}</kbd>
                  </div>
                ))}
              </div>
            )}
            {paletteQuery &&
              !paletteItems.holdings.length &&
              !paletteItems.notes.length &&
              !paletteItems.materials.length &&
              !paletteItems.actions.length && (
                <div className="palette-section">
                  <div className="palette-item">
                    <div className="content">
                      <div className="subtitle">未找到匹配结果</div>
                    </div>
                  </div>
                </div>
              )}
            </div>
            <div className="palette-footer">
              <span>
                <kbd>↑</kbd> <kbd>↓</kbd> 选择
              </span>
              <span>
                <kbd>↵</kbd> 打开
              </span>
              <span>
                <kbd>ESC</kbd> 关闭
              </span>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
