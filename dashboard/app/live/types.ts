export type ConnectionStatus = "connecting" | "connected" | "reconnecting" | "disconnected";
export type Timeframe = "1m" | "5m" | "10m" | "15m" | "30m" | "1h" | "1d" | "1w";
export type SymbolKey = "TMF";
export type StrategyKey = string;

export type KBar = {
  type: "kbar";
  interval: Timeframe;
  symbol: string;
  contract: string;
  exchange_time: string;
  received_time: string;
  latency_ms: number;
  time: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  status: "forming" | "closed";
  connection_status: ConnectionStatus;
  session: "day" | "night";
  trading_date: string;
  no_trade: boolean;
};

export type StatusMessage = {
  type: "status" | "heartbeat";
  symbol: string;
  contract: string;
  connection_status: ConnectionStatus;
  last_tick_time: string | null;
  latency_ms: number | null;
  queue_size: number;
  dropped_ticks: number;
};

export type FeedMessage = KBar | StatusMessage;
export type Ohlc = Pick<KBar, "open" | "high" | "low" | "close"> | null;
export type TradeSelection = {
  symbol: SymbolKey;
  interval: Timeframe;
  strategies: StrategyKey[];
};
export type StrategySignal = {
  strategy: StrategyKey;
  event: "entry" | "exit";
  direction: "long" | "short";
  time: string;
  price: number;
  stop_loss_price: number;
  take_profit_price: number;
  reason: string;
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
export type StrategyResult = {
  key: StrategyKey;
  name: string;
  color: string;
  parameters: Record<string, number>;
  signals: StrategySignal[];
  overlays: StrategyOverlay[];
};
export type StrategyOption = {
  key: StrategyKey;
  name: string;
  category: string;
  description: string;
  color: string;
};
