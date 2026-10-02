export type CurrentUser = {
  role: "researcher" | "trader" | "admin";
  trading_mode: "disabled" | "paper" | "live";
  permissions: string[];
};

export type Account = {
  realized_pnl: number;
  open_contracts: number;
  reserved_contracts: number;
  trades: number;
  kill_switch_active: boolean;
  kill_switch_reason: string | null;
  cooldown_until: string | null;
  recovery_status: "healthy" | "degraded";
  recovery_issues: string[];
  risk_limits: {
    max_position_contracts: number;
    max_risk_per_trade: number;
    max_daily_loss: number;
    max_trades_per_day: number;
    max_consecutive_losses: number;
    cooldown_minutes: number;
  };
};

export type PaperPosition = {
  strategy_id: string;
  strategy_version: number;
  symbol: string;
  contract: string;
  quantity: number;
  average_price: number;
  opened_at: string | null;
  realized_pnl: number;
  unrealized_pnl: number;
  total_cost: number;
  order_source?: "manual" | "strategy_auto";
  runtime_id?: string | null;
  decision_id?: string | null;
  stop_loss_price?: number | null;
  take_profit_price?: number | null;
  strategy_snapshot?: Record<string, unknown> | null;
};

export type PaperOrder = {
  order_id: string;
  submitted_at: string;
  strategy_id: string;
  strategy_version: number;
  contract: string;
  side: "buy" | "sell";
  quantity: number;
  reduce_only: boolean;
  reference_price: number;
  stop_loss_price: number | null;
  status: "pending_risk" | "approved" | "rejected" | "filled";
  status_reason: string;
  execution_timing?: "current_close" | "next_bar_open";
  order_source?: "manual" | "strategy_auto";
  runtime_id?: string | null;
  decision_id?: string | null;
  actual_fill_price?: number | null;
};

export type PaperFill = {
  fill_id: string;
  order_id: string;
  strategy_id: string;
  strategy_version: number;
  symbol: string;
  contract: string;
  side: "buy" | "sell";
  quantity: number;
  price: number;
  commission: number;
  tax: number;
  slippage: number;
  purpose: "entry" | "exit" | "liquidation";
  order_source?: "manual" | "strategy_auto";
  runtime_id?: string | null;
  decision_id?: string | null;
  meta: { occurred_at: string };
};

export type PaperQuote = {
  contract: string;
  close: number;
  received_time: string;
  status: "forming" | "closed";
  session: "day" | "night";
};

export type MarketHealth = {
  service_status: "healthy" | "degraded" | "market_stale" | "provider_disconnected";
  connection_status: string;
  trading_block_reason: "market_stale" | "provider_disconnected" | null;
  stale_after_seconds: number;
  last_tick_time: string | null;
};

export type PaperOverlaySnapshot = {
  positions: PaperPosition[];
  orders: PaperOrder[];
  fills: PaperFill[];
};
