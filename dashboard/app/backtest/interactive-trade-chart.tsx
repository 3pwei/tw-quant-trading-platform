"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import {
  CandlestickSeries,
  ColorType,
  createChart,
  createSeriesMarkers,
  HistogramSeries,
  LineStyle,
  LineSeries,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import { formatPrice, formatTaipeiClock } from "../lib/formatters";
import { prepareStrategySeries } from "./strategy-series";
import {
  backtestChartTime,
  tradeFocusRange,
  type BacktestBar,
  type BacktestTrade,
  type StrategyOverlay,
  type StrategySeries,
  type StrategyVisualization,
  hasDiagnostics,
} from "./trade-chart-model";
import { StrategyParameterSummary, StrategySeriesLegend } from "./strategy-diagnostics";

const taipeiDate = new Intl.DateTimeFormat("zh-TW", {
  timeZone: "Asia/Taipei",
  month: "2-digit",
  day: "2-digit",
});
const taipeiDateClock = new Intl.DateTimeFormat("zh-TW", {
  timeZone: "Asia/Taipei", month: "2-digit", day: "2-digit",
  hour: "2-digit", minute: "2-digit", hourCycle: "h23",
});

function chartTime(value: string): UTCTimestamp {
  return Math.floor(Date.parse(value) / 1000) as UTCTimestamp;
}

function barTime(bar: BacktestBar, rangeMode: boolean): UTCTimestamp {
  return backtestChartTime(bar, rangeMode) as UTCTimestamp;
}

function clock(value: Time, rangeMode: boolean, showDate: boolean): string {
  if (typeof value === "object") {
    const epoch = Date.UTC(value.year, value.month - 1, value.day) / 1000;
    return rangeMode ? taipeiDate.format(epoch * 1000) : showDate ? taipeiDateClock.format(epoch * 1000) : formatTaipeiClock(epoch);
  }
  const epoch = typeof value === "number" ? value : Date.parse(value) / 1000;
  return rangeMode ? taipeiDate.format(epoch * 1000) : showDate ? taipeiDateClock.format(epoch * 1000) : formatTaipeiClock(epoch);
}

function anchorTime(
  bars: BacktestBar[],
  timestamp: string,
  rangeMode: boolean,
): UTCTimestamp {
  const target = Date.parse(timestamp);
  const bar = [...bars].reverse().find(item => Date.parse(item.timestamp) <= target)
    ?? bars[0];
  return barTime(bar, rangeMode);
}

function tradeMarkers(
  bars: BacktestBar[],
  trades: BacktestTrade[],
  selected: BacktestTrade,
  rangeMode: boolean,
): SeriesMarker<Time>[] {
  return trades.flatMap((trade, index) => {
    const active = trade.entry_time === selected.entry_time
      && trade.exit_time === selected.exit_time;
    const number = (trade.trade_index ?? index) + 1;
    const entryColor = active ? "#42d6a4" : "rgba(66,214,164,.5)";
    const exitColor = active ? "#f5b942" : "rgba(245,185,66,.5)";
    return [
      {
        time: anchorTime(bars, trade.entry_time, rangeMode),
        position: trade.direction === "long" ? "belowBar" : "aboveBar",
        color: entryColor,
        shape: trade.direction === "long" ? "arrowUp" : "arrowDown",
        text: active ? `#${number} 進 ${formatPrice(trade.entry_price)}` : `#${number} 進`,
      },
      {
        time: anchorTime(bars, trade.exit_time, rangeMode),
        position: trade.direction === "long" ? "aboveBar" : "belowBar",
        color: exitColor,
        shape: "circle",
        text: active ? `#${number} 出 ${formatPrice(trade.exit_price)}` : `#${number} 出`,
      },
    ] as SeriesMarker<Time>[];
  }).sort((left, right) => Number(left.time) - Number(right.time));
}

type InteractiveTradeChartProps = {
  bars: BacktestBar[];
  trades: BacktestTrade[];
  selectedTrade: BacktestTrade;
  overlays?: StrategyOverlay[];
  visualization?: StrategyVisualization;
  rangeMode?: boolean;
  focusSelection?: boolean;
  showDate?: boolean;
};

export default function InteractiveTradeChart({
  bars,
  trades,
  selectedTrade,
  overlays = [],
  visualization,
  rangeMode = false,
  focusSelection = true,
  showDate = false,
}: InteractiveTradeChartProps) {
  const hostRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volumeRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const markersRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
  const overlayRefs = useRef<Array<ISeriesApi<"Line"> | ISeriesApi<"Histogram">>>([]);
  const riskRefs = useRef<IPriceLine[]>([]);
  const [diagnosticsVisible, setDiagnosticsVisible] = useState(
    hasDiagnostics(visualization),
  );
  const [hovered, setHovered] = useState<{
    event: "entry" | "exit";
    trade: BacktestTrade;
  } | null>(null);
  const showDiagnostics = diagnosticsVisible && hasDiagnostics(visualization);
  const selectedKey = `${selectedTrade.entry_time}:${selectedTrade.exit_time}`;
  const barRevision = useMemo(
    () => `${bars.length}:${bars[0]?.timestamp ?? ""}:${bars.at(-1)?.timestamp ?? ""}`,
    [bars],
  );

  useEffect(() => {
    if (!hostRef.current) return;
    const chart = createChart(hostRef.current, {
      width: hostRef.current.clientWidth,
      height: showDiagnostics ? 610 : 460,
      layout: {
        background: { type: ColorType.Solid, color: "#07120f" },
        textColor: "#9fb0c7",
        attributionLogo: true,
        panes: { separatorColor: "#173027" },
      },
      grid: { vertLines: { color: "#13271f" }, horzLines: { color: "#13271f" } },
      rightPriceScale: { borderColor: "#29463b" },
      timeScale: {
        borderColor: "#29463b",
        timeVisible: !rangeMode,
        secondsVisible: false,
        rightOffset: 4,
        tickMarkFormatter: (value: Time) => clock(value, rangeMode, showDate),
      },
      localization: {
        locale: "zh-TW",
        timeFormatter: (value: Time) => clock(value, rangeMode, showDate),
      },
      handleScale: { mouseWheel: true, pinch: true, axisPressedMouseMove: true },
      handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true },
    });
    const candles = chart.addSeries(CandlestickSeries, {
      upColor: "#42d6a4",
      downColor: "#ff6b72",
      wickUpColor: "#42d6a4",
      wickDownColor: "#ff6b72",
      borderVisible: false,
      priceFormat: { type: "price", precision: 0, minMove: 1 },
    }, 0);
    const volumes = chart.addSeries(HistogramSeries, {
      priceFormat: { type: "volume" },
      priceScaleId: "",
    }, 1);
    chart.panes()[1]?.setHeight(90);
    chartRef.current = chart;
    candleRef.current = candles;
    volumeRef.current = volumes;
    markersRef.current = createSeriesMarkers(candles, []);
    const observer = new ResizeObserver(entries => {
      const width = Math.floor(entries[0]?.contentRect.width ?? 0);
      if (width > 0) chart.applyOptions({
        width,
        height: window.innerWidth < 700
          ? showDiagnostics ? 520 : 400
          : showDiagnostics ? 610 : 460,
      });
    });
    observer.observe(hostRef.current);
    return () => {
      observer.disconnect();
      chart.remove();
      chartRef.current = null;
      candleRef.current = null;
      volumeRef.current = null;
      markersRef.current = null;
      overlayRefs.current = [];
      riskRefs.current = [];
    };
  }, [rangeMode, showDate, showDiagnostics]);

  useEffect(() => {
    const chart = chartRef.current;
    const candles = candleRef.current;
    if (!chart || !candles || !bars.length) return;
    candles.setData(bars.map(bar => ({
      time: barTime(bar, rangeMode),
      open: bar.open,
      high: bar.high,
      low: bar.low,
      close: bar.close,
    })));
    volumeRef.current?.setData(bars.map(bar => ({
      time: barTime(bar, rangeMode),
      value: bar.volume,
      color: bar.close >= bar.open ? "rgba(66,214,164,.4)" : "rgba(255,107,114,.4)",
    })));
    overlayRefs.current.forEach(series => chart.removeSeries(series));
    overlayRefs.current = [];
    const chartTimes = new Map(
      bars.map(bar => [bar.timestamp.slice(0, 16), barTime(bar, rangeMode)]),
    );
    const addGenericSeries = (item: StrategySeries, pane: number) => {
      prepareStrategySeries(item, {
        thresholdRange: {
          from: bars[0].timestamp,
          to: bars[bars.length - 1].timestamp,
        },
        includePoint: point => chartTimes.has(point.time.slice(0, 16)),
      }).forEach(({ points }) => {
        if (!points.length) return;
        if (item.type === "histogram" || item.type === "state") {
          const created = chart.addSeries(HistogramSeries, {
            color: item.color ?? "#64748b",
            priceLineVisible: false,
            lastValueVisible: false,
            ...(item.metadata?.scale_id ? { priceScaleId: String(item.metadata.scale_id) } : {}),
          }, pane);
          created.setData(points.map(point => ({
            time: chartTimes.get(point.time.slice(0, 16)) ?? chartTime(point.time),
            value: point.value,
            color: item.color,
          })));
          overlayRefs.current.push(created);
          return;
        }
        const created = chart.addSeries(LineSeries, {
          color: item.color ?? "#94a3b8",
          lineWidth: item.type === "threshold" ? 1 : 2,
          lineStyle: item.type === "threshold" ? LineStyle.Dashed : LineStyle.Solid,
          priceLineVisible: false,
          lastValueVisible: false,
          title: item.label,
          ...(item.metadata?.scale_id ? { priceScaleId: String(item.metadata.scale_id) } : {}),
        }, pane);
        created.setData(points.map(point => ({
          time: chartTimes.get(point.time.slice(0, 16)) ?? chartTime(point.time),
          value: point.value,
        })));
        overlayRefs.current.push(created);
      });
    };
    if (visualization) {
      visualization.overlays.forEach(item => addGenericSeries(item, 0));
      if (showDiagnostics) {
        visualization.diagnostics.forEach(item => addGenericSeries(item, 2));
        chart.panes()[2]?.setHeight(window.innerWidth < 700 ? 120 : 150);
      }
    } else overlays.filter(item => item.type === "linear_channel").forEach(overlay => {
      const groups = new Map<string, typeof overlay.points>();
      overlay.points
        .filter(point => chartTimes.has(point.time.slice(0, 16)))
        .forEach(point => {
          const key = point.channel_id ?? "channel";
          groups.set(key, [...(groups.get(key) ?? []), point]);
        });
      groups.forEach(points => {
        (["upper", "center", "lower"] as const).forEach(key => {
          const series = chart.addSeries(LineSeries, {
            color: key === "center" ? "rgba(167,139,250,.45)" : "rgba(167,139,250,.8)",
            lineWidth: key === "center" ? 1 : 2,
            priceLineVisible: false,
            lastValueVisible: false,
          }, 0);
          series.setData(points.map(point => ({
            time: chartTimes.get(point.time.slice(0, 16)) ?? chartTime(point.time),
            value: point[key],
          })));
          overlayRefs.current.push(series);
        });
      });
    });
    chart.timeScale().fitContent();
  }, [barRevision, bars, overlays, rangeMode, showDiagnostics, visualization]);

  useEffect(() => {
    const candles = candleRef.current;
    if (!candles || !bars.length) return;
    markersRef.current?.setMarkers(tradeMarkers(bars, trades, selectedTrade, rangeMode));
    riskRefs.current.forEach(line => candles.removePriceLine(line));
    riskRefs.current = [];
    if (selectedTrade.stop_loss_price != null) {
      riskRefs.current.push(candles.createPriceLine({
        price: selectedTrade.stop_loss_price,
        color: "#ff6b72",
        lineWidth: 1,
        lineStyle: 2,
        axisLabelVisible: true,
        title: "停損",
      }));
    }
    if (selectedTrade.take_profit_price != null) {
      riskRefs.current.push(candles.createPriceLine({
        price: selectedTrade.take_profit_price,
        color: "#42d6a4",
        lineWidth: 1,
        lineStyle: 2,
        axisLabelVisible: true,
        title: "停利",
      }));
    }
    const visible = tradeFocusRange(bars, selectedTrade);
    if (focusSelection && visible) {
      chartRef.current?.timeScale().setVisibleLogicalRange(visible);
    } else {
      chartRef.current?.timeScale().fitContent();
    }
  }, [bars, focusSelection, rangeMode, trades, selectedKey, selectedTrade]);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart || !bars.length) return;
    const events = trades.flatMap(trade => [
      { time: anchorTime(bars, trade.entry_time, rangeMode), event: "entry" as const, trade },
      { time: anchorTime(bars, trade.exit_time, rangeMode), event: "exit" as const, trade },
    ]);
    const handler = (param: { time?: Time }) => {
      if (param.time == null) { setHovered(null); return; }
      const match = events.find(item => Number(item.time) === Number(param.time));
      setHovered(match ? { event: match.event, trade: match.trade } : null);
    };
    chart.subscribeCrosshairMove(handler);
    return () => chart.unsubscribeCrosshairMove(handler);
  }, [bars, rangeMode, trades]);

  const hoverContext = hovered?.event === "entry"
    ? hovered.trade.entry_context
    : hovered?.trade.exit_context;

  return <div className="interactive-trade-chart">
    {visualization && <StrategyParameterSummary visualizations={[visualization]} />}
    {visualization && <StrategySeriesLegend visualizations={[visualization]} />}
    <div className="interactive-chart-toolbar">
      <span>滾輪／拖曳／雙指可縮放</span>
      <div>
        {hasDiagnostics(visualization) && <button type="button" onClick={() => setDiagnosticsVisible(value => !value)}>
          {showDiagnostics ? "收合策略診斷" : "展開策略診斷"}
        </button>}
        <button type="button" onClick={() => chartRef.current?.timeScale().fitContent()}>
          顯示全時段
        </button>
      </div>
    </div>
    <div className="interactive-chart-stage">
      <div ref={hostRef} className="interactive-chart-host" aria-label="互動 K 線、策略診斷與交易進出場位置" />
      {hovered && <aside className="strategy-trigger-tooltip">
        <b>{hovered.event === "entry" ? "ENTRY" : "EXIT"} · {hovered.trade.direction.toUpperCase()}</b>
        <span>{hovered.event === "entry" ? hovered.trade.entry_reason ?? "signal_confirmed" : hovered.trade.exit_reason ?? "strategy_exit"}</span>
        {hovered.event === "entry" && <>
          <span>訊號確認 {formatTaipeiClock(hovered.trade.trigger_time ?? hovered.trade.entry_time)}</span>
          <span>成交 {formatTaipeiClock(hovered.trade.entry_time)}</span>
        </>}
        <span>價格 {formatPrice(hovered.event === "entry" ? hovered.trade.entry_price : hovered.trade.exit_price)}</span>
        <span>停損 {formatPrice(hovered.trade.stop_loss_price)} · 停利 {formatPrice(hovered.trade.take_profit_price)}</span>
        {Object.entries(hoverContext ?? {}).slice(0, 5).map(([key, value]) => <span key={key}>{key} = {Number(value).toFixed(4)}</span>)}
      </aside>}
    </div>
  </div>;
}
