import type {
  CandlestickData,
  HistogramData,
  SeriesMarker,
  Time,
  UTCTimestamp,
} from "lightweight-charts";
import type {
  ReplayBar,
  ReplaySignal,
  ReplaySnapshot,
  ReplayStrategy,
  ReplayTradingState,
} from "./types";

export const toReplayChartTime = (value: string) => (
  Math.floor(Date.parse(value) / 1_000) as UTCTimestamp
);

function signalMarker(
  strategy: ReplayStrategy,
  signal: ReplaySignal,
  time: string,
  formatPrice: (value: number) => string,
): SeriesMarker<Time> {
  const entry = signal.event === "entry";
  const long = signal.direction === "long";
  return {
    time: toReplayChartTime(time),
    position: long ? entry ? "belowBar" : "aboveBar" : entry ? "aboveBar" : "belowBar",
    color: entry ? strategy.color : "#f5b942",
    shape: entry ? long ? "arrowUp" : "arrowDown" : "circle",
    text: `${strategy.name} · ${entry ? long ? "多進" : "空進" : "出場"} · ${signal.trigger_reason ?? signal.reason} · ${formatPrice(signal.price)}`,
  };
}

function anchorAtOrBefore(bars: ReplayBar[], occurredAt: string): ReplayBar {
  const target = Date.parse(occurredAt);
  return [...bars].reverse().find(bar => Date.parse(bar.time) <= target) ?? bars[0];
}

export type ReplayChartFrame = {
  candles: CandlestickData<UTCTimestamp>[];
  volumes: HistogramData<UTCTimestamp>[];
  markers: SeriesMarker<Time>[];
};

export function buildReplayChartFrame(
  snapshot: ReplaySnapshot,
  trading: ReplayTradingState | null,
  cursor: number,
  formatPrice: (value: number) => string = String,
): ReplayChartFrame {
  const count = Math.max(1, Math.min(cursor + 1, snapshot.bars.length));
  const bars = snapshot.bars.slice(0, count);
  if (!bars.length) return { candles: [], volumes: [], markers: [] };

  const candles = bars.map(bar => ({
    time: toReplayChartTime(bar.time),
    open: bar.open,
    high: bar.high,
    low: bar.low,
    close: bar.close,
  }));
  const volumes = bars.map(bar => ({
    time: toReplayChartTime(bar.time),
    value: bar.volume,
    color: bar.no_trade
      ? "rgba(148,163,184,.3)"
      : bar.close >= bar.open ? "rgba(45,212,191,.45)" : "rgba(248,113,113,.45)",
  }));
  const now = Date.parse(bars[bars.length - 1].end_time);
  const strategyMarkers = snapshot.strategies.flatMap(strategy => (
    strategy.signals
      .filter(signal => Date.parse(signal.time) <= now)
      .map(signal => signalMarker(
        strategy,
        signal,
        anchorAtOrBefore(bars, signal.time).time,
        formatPrice,
      ))
  ));
  const fillMarkers: SeriesMarker<Time>[] = (trading?.fills ?? [])
    .filter(fill => Date.parse(fill.meta.occurred_at) <= now)
    .map(fill => ({
      time: toReplayChartTime(anchorAtOrBefore(bars, fill.meta.occurred_at).time),
      position: fill.side === "buy" ? "belowBar" : "aboveBar",
      color: fill.side === "buy" ? "#42d6a4" : "#ff6b72",
      shape: fill.side === "buy" ? "arrowUp" : "arrowDown",
      text: `REPLAY ${fill.purpose === "entry" ? "成交" : "平倉"} ${fill.quantity}口 @ ${formatPrice(fill.price)}`,
    }));
  return {
    candles,
    volumes,
    markers: [...strategyMarkers, ...fillMarkers]
      .sort((left, right) => Number(left.time) - Number(right.time)),
  };
}
