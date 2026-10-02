import { useCallback, useEffect, useRef, useState } from "react";
import {
  CandlestickSeries,
  ColorType,
  createChart,
  createSeriesMarkers,
  HistogramSeries,
  LineSeries,
  LineStyle,
  type IChartApi,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type Time,
} from "lightweight-charts";
import { formatPrice, formatTaipeiClock } from "../lib/formatters";
import { buildReplayChartFrame } from "./replay-chart-data";
import type { ReplaySnapshot, ReplayTradingState } from "./types";
import type { ReplaySignal } from "./types";
import type { StrategySeries } from "../backtest/trade-chart-model";
import { prepareStrategySeries } from "../backtest/strategy-series";

function chartClock(value: Time): string {
  if (typeof value === "object") {
    return formatTaipeiClock(Date.UTC(value.year, value.month - 1, value.day) / 1000);
  }
  return formatTaipeiClock(value);
}

type ReplayChartOptions = {
  snapshot: ReplaySnapshot | null;
  trading: ReplayTradingState | null;
  cursor: number;
  diagnosticsVisible?: boolean;
};

export function useReplayChart({ snapshot, trading, cursor, diagnosticsVisible = true }: ReplayChartOptions) {
  const hostRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volumeRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const markerRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
  const strategySeriesRef = useRef<Array<{
    api: ISeriesApi<"Line"> | ISeriesApi<"Histogram">;
    definition: StrategySeries;
    groupKey: string;
  }>>([]);
  const [hoveredSignal, setHoveredSignal] = useState<{
    strategyName: string;
    signal: ReplaySignal;
  } | null>(null);

  useEffect(() => {
    if (!hostRef.current) return;
    const chart = createChart(hostRef.current, {
      width: hostRef.current.clientWidth,
      height: diagnosticsVisible ? 680 : 560,
      layout: {
        background: { type: ColorType.Solid, color: "#07111f" },
        textColor: "#9fb0c7",
        panes: { separatorColor: "#17283b" },
        attributionLogo: true,
      },
      grid: { vertLines: { color: "#132237" }, horzLines: { color: "#132237" } },
      timeScale: {
        borderColor: "#26384d",
        timeVisible: true,
        secondsVisible: false,
        rightOffset: 8,
        tickMarkFormatter: chartClock,
      },
      rightPriceScale: { borderColor: "#26384d" },
      localization: { locale: "zh-TW", timeFormatter: chartClock },
    });
    const candles = chart.addSeries(CandlestickSeries, {
      upColor: "#2dd4bf",
      downColor: "#f87171",
      borderVisible: false,
      wickUpColor: "#2dd4bf",
      wickDownColor: "#f87171",
      priceFormat: { type: "price", precision: 0, minMove: 1 },
    }, 0);
    const volumes = chart.addSeries(HistogramSeries, {
      priceFormat: { type: "volume" },
      priceScaleId: "",
    }, 1);
    chart.panes()[1]?.setHeight(110);
    chartRef.current = chart;
    candleRef.current = candles;
    volumeRef.current = volumes;
    markerRef.current = createSeriesMarkers(candles, []);
    const addSeries = (definition: StrategySeries, pane: number) => {
      const prepared = prepareStrategySeries(definition, snapshot?.bars.length ? {
        thresholdRange: {
          from: snapshot.bars[0].time,
          to: snapshot.bars[snapshot.bars.length - 1].time,
        },
      } : {});
      prepared.forEach(({ groupKey }) => {
        const api = definition.type === "histogram" || definition.type === "state"
          ? chart.addSeries(HistogramSeries, {
              color: definition.color ?? "#64748b", priceLineVisible: false,
              lastValueVisible: false,
              ...(definition.metadata?.scale_id ? { priceScaleId: String(definition.metadata.scale_id) } : {}),
            }, pane)
          : chart.addSeries(LineSeries, {
              color: definition.color ?? "#94a3b8",
              lineWidth: definition.type === "threshold" ? 1 : 2,
              lineStyle: definition.type === "threshold" ? LineStyle.Dashed : LineStyle.Solid,
              priceLineVisible: false, lastValueVisible: false, title: definition.label,
              ...(definition.metadata?.scale_id ? { priceScaleId: String(definition.metadata.scale_id) } : {}),
            }, pane);
        strategySeriesRef.current.push({ api, definition, groupKey });
      });
    };
    snapshot?.strategies.forEach(strategy => {
      strategy.visualization?.overlays.forEach(item => addSeries(item, 0));
      if (diagnosticsVisible) strategy.visualization?.diagnostics.forEach(item => addSeries(item, 2));
    });
    if (strategySeriesRef.current.length) chart.panes()[2]?.setHeight(window.innerWidth < 700 ? 120 : 150);
    const signalAnchors = snapshot?.strategies.flatMap(strategy => strategy.signals.map(signal => {
      const target = Date.parse(signal.time);
      const anchor = [...(snapshot?.bars ?? [])].reverse().find(bar => Date.parse(bar.time) <= target);
      return { time: anchor ? toReplayTime(anchor.time) : null, strategyName: strategy.name, signal };
    })) ?? [];
    const hoverHandler = (param: { time?: Time }) => {
      if (param.time == null) { setHoveredSignal(null); return; }
      const match = signalAnchors.find(item => item.time != null && Number(item.time) === Number(param.time));
      setHoveredSignal(match ? { strategyName: match.strategyName, signal: match.signal } : null);
    };
    chart.subscribeCrosshairMove(hoverHandler);
    const observer = new ResizeObserver(entries => {
      const width = Math.floor(entries[0]?.contentRect.width ?? 0);
      if (width > 0) {
        chart.applyOptions({ width, height: window.innerWidth < 700
          ? diagnosticsVisible ? 560 : 460
          : diagnosticsVisible ? 680 : 560 });
      }
    });
    observer.observe(hostRef.current);
    return () => {
      observer.disconnect();
      chart.unsubscribeCrosshairMove(hoverHandler);
      chart.remove();
      chartRef.current = null;
      candleRef.current = null;
      volumeRef.current = null;
      markerRef.current = null;
      strategySeriesRef.current = [];
    };
  }, [diagnosticsVisible, snapshot]);

  const paint = useCallback((nextCursor: number, fit = false) => {
    if (!snapshot?.bars.length) return;
    const frame = buildReplayChartFrame(snapshot, trading, nextCursor, formatPrice);
    candleRef.current?.setData(frame.candles);
    volumeRef.current?.setData(frame.volumes);
    markerRef.current?.setMarkers(frame.markers);
    const now = Date.parse(snapshot.bars[Math.max(0, Math.min(nextCursor, snapshot.bars.length - 1))].end_time);
    const cursorBar = snapshot.bars[Math.max(0, Math.min(nextCursor, snapshot.bars.length - 1))];
    strategySeriesRef.current.forEach(({ api, definition, groupKey }) => {
      const prepared = prepareStrategySeries(definition, {
        thresholdRange: { from: snapshot.bars[0].time, to: cursorBar.time },
        includePoint: point => Date.parse(point.time) <= now,
      }).find(item => item.groupKey === groupKey);
      const data = (prepared?.points ?? [])
        .map(point => ({ time: toReplayTime(point.time), value: point.value }));
      api.setData(data);
    });
    if (fit) chartRef.current?.timeScale().fitContent();
    else chartRef.current?.timeScale().scrollToRealTime();
  }, [snapshot, trading]);

  useEffect(() => {
    paint(cursor);
  }, [cursor, paint]);

  return { hostRef, paint, hoveredSignal };
}

const toReplayTime = (value: string) => Math.floor(Date.parse(value) / 1000) as import("lightweight-charts").UTCTimestamp;
