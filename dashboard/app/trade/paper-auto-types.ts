import type { Timeframe } from "../live/types";

export type WorkspaceTradingMode = "observe" | "manual_paper" | "paper_auto";
export type RuntimeStatus = "active" | "paused" | "armed" | "recovery_locked" | "stopped";

export type TradingRuntime = {
  runtime_id: string;
  owner_user_id: string;
  strategy_kind: "atomic" | "composite";
  strategy_id: string;
  strategy_version: number | null;
  strategy_snapshot: {
    strategy?: string;
    parameters?: Record<string, number>;
    interval?: Timeframe;
    strategy_id?: string;
    version?: number;
    definition?: Record<string, unknown>;
  };
  symbol: string;
  interval: Timeframe;
  quantity: number;
  mode: "observe" | "paper_auto";
  status: RuntimeStatus;
  recovery_issue: string | null;
  recovery_checked_at: string | null;
  last_evaluated_bar: string | null;
  last_decision: string | null;
  created_at: string;
  updated_at: string;
};

export type TradingDecision = {
  decision_id: string;
  runtime_id: string;
  strategy_id: string;
  strategy_version: number | null;
  symbol: string;
  contract: string;
  interval: Timeframe;
  trigger_time: string;
  direction: "long" | "short";
  action: "entry" | "exit" | "none";
  reason: string;
  context: Record<string, unknown>;
  source_bar_time: string;
  source_bar_id?: string | null;
  execution_status: "pending" | "submitted" | "filled" | "skipped" | "rejected" | "not_applicable";
  execution_reason: string | null;
  order_id: string | null;
  reference_price: number | null;
  planned_stop_price: number | null;
  actual_fill_price: number | null;
  created_at: string;
};

export type RuntimeAction = "arm" | "pause" | "stop";

