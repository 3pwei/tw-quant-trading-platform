import { apiUrl } from "../lib/api-client.ts";
import type { BacktestBar, BacktestTrade, StrategyOverlay, StrategyVisualization } from "../backtest/trade-chart-model";

export type DemoCase = { id: string; label: string; synthetic_data: boolean; simulated_trades: boolean };
export type DemoResult = {
  case_id: string; synthetic_data: true; simulated_trades: true; performance_claim: false;
  date_range: string;
  config: { initial_capital: number; quantity: number; stop_loss_pct: number; take_profit_pct: number; commission_per_side: number; slippage_points: number; contract_multiplier: number };
  summary: { net_profit: number; return_pct: number; ending_equity: number; max_drawdown: number; max_drawdown_pct: number; win_rate_pct: number; profit_factor: number | null; daily_sharpe: number | null; total_cost: number };
  equity: { timestamp: string; equity: number; net_pnl: number; peak: number; drawdown: number; drawdown_pct: number }[];
  bars: BacktestBar[]; trades: (BacktestTrade & { total_cost: number; holding_minutes: number; mfe: number; mae: number })[];
  visualization: StrategyVisualization; overlays: StrategyOverlay[];
};

export type DemoError = "unavailable" | "limited" | "timeout" | "network" | "invalid";
export class DemoRequestError extends Error {
  readonly kind: DemoError;
  constructor(kind: DemoError) { super(kind); this.kind = kind; }
}

async function readResponse(response: Response): Promise<unknown> {
  if (!response.ok) {
    if (response.status === 429) throw new DemoRequestError("limited");
    if (response.status === 503) throw new DemoRequestError("timeout");
    throw new DemoRequestError("unavailable");
  }
  try { return await response.json(); }
  catch { throw new DemoRequestError("invalid"); }
}

async function request(path: string, init: RequestInit): Promise<unknown> {
  try { return await readResponse(await fetch(apiUrl(path), { ...init, cache: "no-store" })); }
  catch (error) {
    if (error instanceof DemoRequestError) throw error;
    throw new DemoRequestError("network");
  }
}

export async function loadDemoCases(): Promise<DemoCase[]> {
  const body = await request("/api/demo/cases", { method: "GET" });
  if (!body || typeof body !== "object" || !Array.isArray((body as { cases?: unknown }).cases)) {
    throw new DemoRequestError("invalid");
  }
  return (body as { cases: DemoCase[] }).cases.filter(item =>
    typeof item.id === "string" && typeof item.label === "string"
    && item.synthetic_data === true && item.simulated_trades === true);
}

export function prepareDemoResult(body: unknown, caseId: string): DemoResult {
  if (!body || typeof body !== "object") throw new DemoRequestError("invalid");
  const value = body as DemoResult;
  if (value.case_id !== caseId || value.synthetic_data !== true
    || value.simulated_trades !== true || value.performance_claim !== false
    || typeof value.date_range !== "string" || !value.config || !value.summary
    || !Array.isArray(value.equity)
    || !Array.isArray(value.bars) || !Array.isArray(value.trades)
    || !value.visualization || !Array.isArray(value.visualization.diagnostics)
    || !Array.isArray(value.visualization.overlays)
    || !Array.isArray(value.visualization.parameters)) throw new DemoRequestError("invalid");
  return {
    ...value,
    overlays: Array.isArray(value.overlays) ? value.overlays : [],
    trades: value.trades.map((trade, index) => ({ ...trade, trade_index: index })),
  };
}

export async function runDemoCase(caseId: string): Promise<DemoResult> {
  // The only client-controlled field accepted by the capability is case_id.
  const body = await request("/api/demo/backtests", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ case_id: caseId }),
  });
  return prepareDemoResult(body, caseId);
}

export function demoErrorMessage(error: unknown): string {
  if (!(error instanceof DemoRequestError)) return "展示服務暫時無法連線，請稍後重試。";
  switch (error.kind) {
    case "limited": return "操作過於頻繁或目前有其他展示正在執行，請稍候再試。";
    case "timeout": return "展示執行逾時或服務暫時忙碌，請稍候重試。";
    case "invalid": return "展示資料暫時無法顯示，請稍後重試。";
    default: return "展示服務目前無法使用，請稍後重試。";
  }
}
