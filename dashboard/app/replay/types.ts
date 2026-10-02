export type Session = "day" | "night";

export type ReplayBar = {
  time: string; end_time: string; open: number; high: number; low: number;
  close: number; volume: number; contract: string; session: Session;
  trading_date: string; no_trade: boolean;
};

export type ReplaySignal = {
  strategy: string; event: "entry" | "exit"; direction: "long" | "short";
  time: string; price: number; stop_loss_price: number;
  take_profit_price: number; reason: string;
  trigger_time?: string;
  trigger_reason?: string;
  context?: Record<string, number>;
};

export type ReplayStrategy = {
  key: string; name: string; color: string; kind?: "composite";
  version?: number; signals: ReplaySignal[];
  visualization?: import("../backtest/trade-chart-model").StrategyVisualization;
  parameters?: Record<string, number>;
};

export type ReplayAccount = {
  realized_pnl: number; open_contracts: number; trades: number;
};

export type ReplayPosition = {
  strategy_id: string; strategy_version: number; contract: string;
  quantity: number; average_price: number; unrealized_pnl: number;
};

export type ReplayOrder = {
  order_id: string; submitted_at: string; strategy_id: string;
  strategy_version: number; contract: string; side: "buy" | "sell";
  quantity: number; reduce_only: boolean; reference_price: number;
  stop_loss_price: number | null;
  status: "pending_risk" | "approved" | "rejected" | "filled";
  status_reason: string;
};

export type ReplayFill = {
  fill_id: string; contract: string; side: "buy" | "sell"; quantity: number;
  price: number; commission: number; tax: number; slippage: number;
  purpose: "entry" | "exit" | "liquidation"; meta: { occurred_at: string };
};

export type ReplayTradingState = {
  session_id: string; snapshot_id: string; mode: "replay";
  isolated_from_live_paper: true; cursor: number; bar_count: number;
  virtual_time: string; rewound: boolean; account: ReplayAccount;
  positions: ReplayPosition[]; orders: ReplayOrder[]; fills: ReplayFill[];
};

export type ReplaySnapshot = {
  snapshot_id: string; created_at: string; symbol: string; trading_date: string;
  session: Session; interval: string; interval_name: string; bars: ReplayBar[];
  strategies: ReplayStrategy[]; trading_session: ReplayTradingState;
};

export type StrategyOption = {
  key: string; name: string; kind: "atomic" | "composite"; color: string;
};

export type Availability = {
  date: string; sessions: { key: Session; bar_count: number }[];
};

export type ReplayOptions = {
  available_start: string | null; available_end: string | null;
  available_dates: Availability[]; intervals: { key: string; name: string }[];
  strategies: StrategyOption[]; max_strategies: number;
  sessions: { key: Session; name: string }[];
};
