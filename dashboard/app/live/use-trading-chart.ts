import { useCallback, useEffect, useRef, useState } from "react";
import {
  CandlestickSeries,
  ColorType,
  createChart,
  createSeriesMarkers,
  HistogramSeries,
  LineSeries,
  LineStyle,
  type CandlestickData,
  type HistogramData,
  type IChartApi,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import { formatPrice } from "../lib/formatters";
import type { PaperOverlaySnapshot } from "../paper/types";
import type { KBar, LinearChannelPoint, Ohlc, StrategyResult } from "./types";
import { usePaperOverlay } from "./use-paper-overlay";

export const toChartTime = (value: string): UTCTimestamp => (
  Math.floor(Date.parse(value) / 1000) as UTCTimestamp
);

const chartTimeFormatter = new Intl.DateTimeFormat("zh-TW", {
  timeZone: "Asia/Taipei",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});

function formatChartTime(value: Time): string {
  if (typeof value === "number") return chartTimeFormatter.format(new Date(value * 1000));
  if (typeof value === "string") return chartTimeFormatter.format(new Date(value));
  return chartTimeFormatter.format(new Date(Date.UTC(value.year, value.month - 1, value.day)));
}

function candle(bar: KBar): CandlestickData<UTCTimestamp> {
  const forming = bar.status === "forming";
  return {
    time: toChartTime(bar.time),
    open: bar.open,
    high: bar.high,
    low: bar.low,
    close: bar.close,
    ...(forming ? { color: "#f5b942", wickColor: "#f5b942", borderColor: "#f5b942" } : {}),
  };
}

function volume(bar: KBar): HistogramData<UTCTimestamp> {
  return {
    time: toChartTime(bar.time),
    value: bar.volume,
    color: bar.no_trade
      ? "rgba(148,163,184,.3)"
      : bar.close >= bar.open ? "rgba(45,212,191,.45)" : "rgba(248,113,113,.45)",
  };
}

type TradingChartOptions = {
  strategyResults: StrategyResult[];
  paperOverlay: PaperOverlaySnapshot;
  symbol: string;
  latest: KBar | null;
};

export function useTradingChart({
  strategyResults,
  paperOverlay,
  symbol,
  latest,
}: TradingChartOptions) {
  const hostRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volumeRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const markerRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
  const channelSeriesRef = useRef<ISeriesApi<"Line">[]>([]);
  const barTimesRef = useRef<UTCTimestamp[]>([]);
  const [crosshair, setCrosshair] = useState<Ohlc>(null);
  const [historyCount, setHistoryCount] = useState(0);
  const fillMarkers = usePaperOverlay({
    snapshot: paperOverlay,
    symbol,
    contract: latest?.contract,
    barTimesRef,
    candleRef,
    barRevision: `${historyCount}:${latest?.time ?? ""}`,
  });

  useEffect(() => {
    if (!hostRef.current) return;
    const chart = createChart(hostRef.current, {
      autoSize: false,
      width: hostRef.current.clientWidth,
      height: window.matchMedia("(max-width: 840px)").matches ? 500 : 610,
      layout: {
        background: { type: ColorType.Solid, color: "#07111f" },
        textColor: "#9fb0c7",
        panes: { separatorColor: "#17283b" },
        attributionLogo: true,
      },
      grid: { vertLines: { color: "#132237" }, horzLines: { color: "#132237" } },
      crosshair: { vertLine: { color: "#94a3b8" }, horzLine: { color: "#94a3b8" } },
      timeScale: {
        borderColor: "#26384d",
        timeVisible: true,
        secondsVisible: false,
        rightOffset: 6,
        tickMarkFormatter: formatChartTime,
      },
      rightPriceScale: { borderColor: "#26384d" },
      localization: { locale: "zh-TW", timeFormatter: formatChartTime },
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
    chart.panes()[1]?.setHeight(130);
    chart.subscribeCrosshairMove(param => {
      const value = param.seriesData.get(candles) as CandlestickData<Time> | undefined;
      setCrosshair(value && "open" in value
        ? { open: value.open, high: value.high, low: value.low, close: value.close }
        : null);
    });
    chartRef.current = chart;
    candleRef.current = candles;
    volumeRef.current = volumes;
    markerRef.current = createSeriesMarkers(candles, []);
    const observer = new ResizeObserver(entries => {
      const width = Math.floor(entries[0]?.contentRect.width ?? 0);
      if (width > 0) chart.applyOptions({ width });
    });
    observer.observe(hostRef.current);
    return () => {
      observer.disconnect();
      chart.remove();
      chartRef.current = null;
      candleRef.current = null;
      volumeRef.current = null;
      markerRef.current = null;
      channelSeriesRef.current = [];
    };
  }, []);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    channelSeriesRef.current.forEach(series => chart.removeSeries(series));
    channelSeriesRef.current = [];
    const overlay = strategyResults
      .flatMap(strategy => strategy.overlays ?? [])
      .find(item => item.type === "linear_channel");
    if (!overlay?.points.length) return;
    const definitions: {
      key: "upper" | "center" | "lower";
      color: string;
      style: LineStyle;
    }[] = [
      { key: "upper", color: "#fbbf24", style: LineStyle.Solid },
      { key: "center", color: "rgba(251,191,36,.65)", style: LineStyle.Dashed },
      { key: "lower", color: "#fbbf24", style: LineStyle.Solid },
    ];
    const segments = new Map<string, LinearChannelPoint[]>();
    overlay.points.forEach(point => {
      const key = point.channel_id ?? "channel";
      segments.set(key, [...(segments.get(key) ?? []), point]);
    });
    channelSeriesRef.current = [...segments.values()].flatMap(points => (
      definitions.map(definition => {
        const series = chart.addSeries(LineSeries, {
          color: definition.color,
          lineWidth: definition.key === "center" ? 1 : 2,
          lineStyle: definition.style,
          priceLineVisible: false,
          lastValueVisible: false,
          crosshairMarkerVisible: false,
        }, 0);
        series.setData(points.map(point => ({
          time: toChartTime(point.time),
          value: point[definition.key],
        })));
        return series;
      })
    ));
  }, [strategyResults]);

  useEffect(() => {
    const strategyMarkers: SeriesMarker<Time>[] = strategyResults.flatMap(strategy => (
      strategy.signals.map(signal => ({
        time: toChartTime(signal.time),
        position: signal.direction === "long"
          ? signal.event === "entry" ? "belowBar" as const : "aboveBar" as const
          : signal.event === "entry" ? "aboveBar" as const : "belowBar" as const,
        color: signal.event === "entry" ? strategy.color : "#f59e0b",
        shape: signal.event === "entry"
          ? signal.direction === "long" ? "arrowUp" as const : "arrowDown" as const
          : "circle" as const,
        text: signal.event === "entry"
          ? `${strategy.key.toUpperCase()} ${signal.direction === "long" ? "多" : "空"}進 · SL ${formatPrice(signal.stop_loss_price)} · TP ${formatPrice(signal.take_profit_price)}`
          : `${strategy.key.toUpperCase()} ${signal.direction === "long" ? "多" : "空"}出`,
      }))
    ));
    markerRef.current?.setMarkers(
      [...strategyMarkers, ...fillMarkers].sort((a, b) => Number(a.time) - Number(b.time)),
    );
  }, [fillMarkers, strategyResults]);

  const setHistory = useCallback((bars: KBar[]) => {
    candleRef.current?.setData(bars.map(candle));
    volumeRef.current?.setData(bars.map(volume));
    barTimesRef.current = bars.map(bar => toChartTime(bar.time));
    setHistoryCount(bars.length);
  }, []);

  const update = useCallback((bar: KBar) => {
    candleRef.current?.update(candle(bar));
    volumeRef.current?.update(volume(bar));
    const time = toChartTime(bar.time);
    if (barTimesRef.current.at(-1) !== time) barTimesRef.current.push(time);
  }, []);

  const reset = useCallback(() => {
    setCrosshair(null);
    setHistoryCount(0);
    barTimesRef.current = [];
    candleRef.current?.setData([]);
    volumeRef.current?.setData([]);
    markerRef.current?.setMarkers([]);
  }, []);

  return { hostRef, crosshair, historyCount, setHistory, update, reset };
}
