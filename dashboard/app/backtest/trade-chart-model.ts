export type BacktestBar = {
  timestamp: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  contract?: string;
  session?: string;
  trading_date?: string;
};

export type BacktestTrade = {
  trade_index?: number;
  direction: "long" | "short";
  trigger_time?: string;
  entry_time: string;
  exit_time: string;
  entry_price: number;
  exit_price: number;
  net_pnl: number;
  contract?: string;
  session?: string;
  trading_date?: string;
  stop_loss_price?: number;
  take_profit_price?: number;
  entry_reason?: string;
  exit_reason?: string;
  entry_context?: Record<string, number>;
  exit_context?: Record<string, number>;
};

export type StrategySeriesPoint = {
  time: string;
  value: number;
  group?: string;
};

export type StrategySeries = {
  key: string;
  label: string;
  panel: "price" | "strategy";
  type: "line" | "histogram" | "threshold" | "state";
  color?: string;
  points: StrategySeriesPoint[];
  metadata?: Record<string, unknown>;
};

export type StrategyParameter = {
  key: string;
  label: string;
  value: number;
  display_value: number | string;
  unit?: string;
  important?: boolean;
};

export type StrategyVisualization = {
  schema_version: number;
  strategy: { key: string; name: string };
  parameters: StrategyParameter[];
  panels: { key: string; label: string; order: number; collapsible?: boolean; default_visible?: boolean }[];
  overlays: StrategySeries[];
  diagnostics: StrategySeries[];
};

export type LinearChannelPoint = {
  time: string;
  upper: number;
  center: number;
  lower: number;
  slope: number;
  direction?: "up" | "down";
  channel_id?: string;
};

export type StrategyOverlay = {
  type: "linear_channel";
  model?: "dow_theory";
  points: LinearChannelPoint[];
};

export type BacktestChartScope = {
  key: string;
  kind: "session" | "range";
  label: string;
  interval: string;
  contract: string;
  trading_date?: string;
  session?: "day" | "night";
};

export type BacktestChartPayload = {
  scope: BacktestChartScope;
  bars: BacktestBar[];
  trades: BacktestTrade[];
  overlays: StrategyOverlay[];
  visualization?: StrategyVisualization;
};

export function hasDiagnostics(
  visualization: StrategyVisualization | null | undefined,
): boolean {
  return Boolean(visualization?.diagnostics.some(series => series.points.length));
}

export function isRangeInterval(interval: string): boolean {
  return interval === "1d" || interval === "1w";
}

export function backtestChartTime(
  bar: BacktestBar,
  rangeMode: boolean,
): number {
  const value = rangeMode && bar.trading_date
    ? `${bar.trading_date}T00:00:00Z`
    : bar.timestamp;
  return Math.floor(Date.parse(value) / 1000);
}

export function scopeKeyForTrade(
  trade: BacktestTrade,
  interval: string,
): string {
  const contract = trade.contract ?? "contract";
  if (isRangeInterval(interval)) return `range:${interval}:${contract}`;
  return [
    "session",
    interval === "multi" ? "1m" : interval,
    contract,
    trade.trading_date ?? trade.entry_time.slice(0, 10),
    trade.session ?? "unknown",
  ].join(":");
}

export function barsForTrade(
  bars: BacktestBar[],
  trade: BacktestTrade,
  interval: string,
): BacktestBar[] {
  const contract = trade.contract;
  if (isRangeInterval(interval)) {
    return bars.filter(bar => !contract || !bar.contract || bar.contract === contract);
  }
  return bars.filter(bar => (
    (!contract || !bar.contract || bar.contract === contract)
    && (!trade.trading_date || bar.trading_date === trade.trading_date)
    && (!trade.session || bar.session === trade.session)
  ));
}

export function tradesForScope(
  trades: BacktestTrade[],
  selected: BacktestTrade,
  interval: string,
): BacktestTrade[] {
  const selectedKey = scopeKeyForTrade(selected, interval);
  return trades.filter(trade => scopeKeyForTrade(trade, interval) === selectedKey);
}

function nearestBarIndex(bars: BacktestBar[], timestamp: string): number {
  const target = Date.parse(timestamp);
  let nearest = 0;
  let distance = Number.POSITIVE_INFINITY;
  bars.forEach((bar, index) => {
    const nextDistance = Math.abs(Date.parse(bar.timestamp) - target);
    if (nextDistance < distance) {
      nearest = index;
      distance = nextDistance;
    }
  });
  return nearest;
}

export function tradeFocusRange(
  bars: BacktestBar[],
  trade: BacktestTrade,
  padding = 12,
  minimumBars = 36,
): { from: number; to: number } | null {
  if (!bars.length) return null;
  const entry = nearestBarIndex(bars, trade.entry_time);
  const exit = nearestBarIndex(bars, trade.exit_time);
  let from = Math.max(0, Math.min(entry, exit) - padding);
  let to = Math.min(bars.length - 1, Math.max(entry, exit) + padding);
  const missing = minimumBars - (to - from + 1);
  if (missing > 0) {
    const before = Math.min(from, Math.ceil(missing / 2));
    from -= before;
    to = Math.min(bars.length - 1, to + missing - before);
    from = Math.max(0, from - Math.max(0, minimumBars - (to - from + 1)));
  }
  return { from, to };
}
