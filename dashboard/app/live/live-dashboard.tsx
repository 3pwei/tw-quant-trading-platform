"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import SystemNav from "../components/system-nav";
import { apiRequest } from "../lib/api-client";
import { formatPrice, formatTaipeiDateTime } from "../lib/formatters";
import PaperTradingDashboard from "../paper/paper-trading-dashboard";
import type { MarketHealth, PaperOverlaySnapshot } from "../paper/types";
import type {
  KBar,
  StrategyKey,
  StrategyOption,
  StrategyResult,
  SymbolKey,
  Timeframe,
  TradeSelection,
} from "./types";
import { useMarketSocket } from "./use-market-socket";
import { useTradingChart } from "./use-trading-chart";
import { closeStrategyMenuWhenOutside } from "./strategy-menu";
import { createInitialTradeSelection } from "./trade-selection";
import LiveShadowPanel from "../trade/live-shadow-panel";
import LiveCanaryPanel from "../trade/live-canary-panel";
import LiveAutoPanel from "../trade/live-auto-panel";
const TIMEFRAME_OPTIONS: { key: Timeframe; name: string }[] = [
  { key: "1m", name: "1 分 K" }, { key: "5m", name: "5 分 K" },
  { key: "10m", name: "10 分 K" }, { key: "15m", name: "15 分 K" },
  { key: "30m", name: "30 分 K" }, { key: "1h", name: "1 小時 K" },
  { key: "1d", name: "日 K" }, { key: "1w", name: "週 K" },
];
const PRODUCT_OPTIONS: { key: SymbolKey; name: string }[] = [
  { key: "TMF", name: "微型臺指期貨" },
];
const EMPTY_PAPER_OVERLAY: PaperOverlaySnapshot = {
  positions: [], orders: [], fills: [],
};

function StrategyStatus({ strategy }: { strategy: StrategyResult }) {
  const latestEntry = [...strategy.signals].reverse().find(signal => signal.event === "entry");
  return <div className="strategy-card">
    <div className="strategy-card-title">
      <i style={{ background: strategy.color }} />
      <span>{strategy.name}</span>
      <b>{strategy.signals.length} 個訊號</b>
    </div>
    <div className="strategy-risk-levels">
      <span>進場 <b>{formatPrice(latestEntry?.price)}</b></span>
      <span className="stop">停損 <b>{formatPrice(latestEntry?.stop_loss_price)}</b></span>
      <span className="target">停利 <b>{formatPrice(latestEntry?.take_profit_price)}</b></span>
    </div>
  </div>;
}

export default function TradingWorkspace() {
  const strategyRequest = useRef(0);
  const strategyLoaderRef = useRef<() => Promise<void>>(async () => undefined);
  const strategyMenuRef = useRef<HTMLDetailsElement>(null);
  const [latest, setLatest] = useState<KBar | null>(null);
  const [error, setError] = useState("");
  const [selection, setSelection] = useState<TradeSelection>(
    createInitialTradeSelection,
  );
  const [strategyOptions, setStrategyOptions] = useState<StrategyOption[]>([]);
  const [strategyResults, setStrategyResults] = useState<StrategyResult[]>([]);
  const [marketHealth, setMarketHealth] = useState<MarketHealth | null>(null);
  const [clock, setClock] = useState(() => Date.now());
  const [paperOverlay, setPaperOverlay] = useState<PaperOverlaySnapshot>(EMPTY_PAPER_OVERLAY);
  const [settingsOpen, setSettingsOpen] = useState(false);

  useEffect(() => {
    const closeStrategyMenu = (event: PointerEvent) => {
      closeStrategyMenuWhenOutside(strategyMenuRef.current, event.target);
    };
    document.addEventListener("pointerdown", closeStrategyMenu);
    return () => document.removeEventListener("pointerdown", closeStrategyMenu);
  }, []);

  const selectedInterval = selection.interval;
  const selectedStrategies = selection.strategies;
  const updatePaperOverlay = useCallback((snapshot: PaperOverlaySnapshot) => {
    setPaperOverlay(snapshot);
  }, []);
  const {
    hostRef,
    crosshair,
    historyCount,
    setHistory,
    update: updateChart,
    reset: resetChart,
  } = useTradingChart({
    strategyResults,
    paperOverlay,
    symbol: selection.symbol,
    latest,
  });

  useEffect(() => {
    if (!settingsOpen) return;
    const previousOverflow = document.body.style.overflow;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setSettingsOpen(false);
    };
    document.body.style.overflow = "hidden";
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.body.style.overflow = previousOverflow;
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [settingsOpen]);

  useEffect(() => {
    let active = true;
    apiRequest<{ strategies: StrategyOption[] }>(
      "/api/strategies",
      { cache: "no-store" },
      "策略清單載入失敗",
    )
      .then(body => {
        if (active) setStrategyOptions(body.strategies);
      })
      .catch(reason => { if (active) setError(reason instanceof Error ? reason.message : "策略清單載入失敗"); });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    let active = true;
    const loadHealth = async () => {
      try {
        const body = await apiRequest<MarketHealth>(
          "/api/health",
          { cache: "no-store" },
          "健康狀態載入失敗",
        );
        if (active) setMarketHealth(body);
      } catch {
        if (active) setMarketHealth(null);
      }
    };
    void loadHealth();
    const healthTimer = window.setInterval(() => void loadHealth(), 5_000);
    const clockTimer = window.setInterval(() => setClock(Date.now()), 1_000);
    return () => {
      active = false;
      window.clearInterval(healthTimer);
      window.clearInterval(clockTimer);
    };
  }, []);

  const loadHistory = useCallback(async (
    symbol: SymbolKey,
    interval: Timeframe,
    signal: AbortSignal,
  ) => {
    const bars = await apiRequest<KBar[]>(
      `/api/kbars?symbol=${symbol}&interval=${interval}&limit=500`,
      { cache: "no-store", signal },
      "歷史 K 棒載入失敗",
    );
    if (signal.aborted) return [];
    if (bars.some(bar => bar.interval !== interval)) {
      throw new Error(`歷史 K 棒週期不符（預期 ${interval}）`);
    }
    setHistory(bars);
    return bars;
  }, [setHistory]);

  const loadStrategySignals = useCallback(async () => {
    const requestId = ++strategyRequest.current;
    if (!selectedStrategies.length) {
      setStrategyResults([]);
      return;
    }
    const selected = selectedStrategies.join(",");
    const payload = await apiRequest<{ strategies: StrategyResult[] }>(
      `/api/strategy-signals?symbol=${selection.symbol}&strategies=${selected}&interval=${selectedInterval}&limit=500`,
      { cache: "no-store" },
      "策略訊號載入失敗",
    );
    if (requestId !== strategyRequest.current) return;
    setStrategyResults(payload.strategies);
  }, [selectedStrategies, selectedInterval, selection.symbol]);

  useEffect(() => {
    strategyLoaderRef.current = async () => {
      await loadStrategySignals().catch(reason => {
        setError(reason instanceof Error ? reason.message : "策略訊號載入失敗");
      });
    };
    void strategyLoaderRef.current();
  }, [loadStrategySignals]);

  const clearStrategies = useCallback(() => setStrategyResults([]), []);
  const reloadStrategies = useCallback(
    () => strategyLoaderRef.current(),
    [],
  );
  const reportError = useCallback((message: string) => setError(message), []);
  const { status, lastTick, latency } = useMarketSocket({
    symbol: selection.symbol,
    interval: selectedInterval,
    loadHistory,
    resetChart,
    updateChart,
    clearStrategies,
    reloadStrategies,
    reportError,
    setLatest,
  });

  const toggleStrategy = (key: StrategyKey) => {
    setSelection(current => ({
      ...current,
      strategies: current.strategies.includes(key)
        ? current.strategies.filter(value => value !== key)
        : [...current.strategies, key],
    }));
  };

  const shown = crosshair ?? latest;
  const quoteAgeSeconds = latest
    ? Math.max(0, Math.floor((clock - new Date(latest.received_time).getTime()) / 1_000))
    : null;
  const quoteFresh = Boolean(
    latest
    && status === "connected"
    && marketHealth?.trading_block_reason == null
    && quoteAgeSeconds != null
    && quoteAgeSeconds <= (marketHealth?.stale_after_seconds ?? 30),
  );
  return <main className="live-shell">
    <header className="live-header">
      <div><span>MILESPAPA QUANT LAB · TRADE WORKSPACE</span><h1>{selection.symbol} 交易工作台</h1></div>
      <button className="mobile-trade-settings-trigger" type="button" aria-haspopup="dialog" aria-expanded={settingsOpen} onClick={() => setSettingsOpen(true)}>交易設定</button>
      <div className="live-header-actions">
        <label className="timeframe-select"><span>商品</span><select value={selection.symbol} onChange={event => setSelection(current => ({ ...current, symbol: event.target.value as SymbolKey }))}>{PRODUCT_OPTIONS.map(option => <option key={option.key} value={option.key}>{option.key} · {option.name}</option>)}</select></label>
        <label className="timeframe-select"><span>K 棒週期</span><select value={selectedInterval} onChange={event => setSelection(current => ({ ...current, interval: event.target.value as Timeframe }))}>{TIMEFRAME_OPTIONS.map(option => <option key={option.key} value={option.key}>{option.name}</option>)}</select></label>
        <details ref={strategyMenuRef} className="strategy-select">
          <summary>交易策略 <b>{selectedStrategies.length}</b></summary>
          <div className="strategy-menu">
            <span>MULTI-SELECT · 訊號分開疊加</span>
            {strategyOptions.map(option => <label key={option.key}>
              <input type="checkbox" checked={selectedStrategies.includes(option.key)} onChange={() => toggleStrategy(option.key)} />
              <i style={{ background: option.color }} />
              <span><b>{option.name}</b><small>{option.category} · {option.description}</small></span>
            </label>)}
          </div>
        </details>
        <div className="paper-mode-pill">PAPER</div>
        <div className={`freshness-pill ${quoteFresh ? "fresh" : "stale"}`}>報價 {quoteAgeSeconds == null ? "等待中" : `${quoteAgeSeconds} 秒前`}</div>
        <div className={`connection-pill ${status}`}><i />{status === "connected" ? "即時連線" : status === "reconnecting" ? "重新連線中" : status === "connecting" ? "連線中" : "行情中斷"}</div>
      </div>
    </header>
    {settingsOpen && <>
      <button className="trade-settings-backdrop" type="button" aria-label="關閉交易設定" onClick={() => setSettingsOpen(false)} />
      <section className="trade-settings-sheet" role="dialog" aria-modal="true" aria-labelledby="trade-settings-title">
        <header>
          <div><span>TRADING SETTINGS</span><h2 id="trade-settings-title">交易設定</h2></div>
          <button type="button" aria-label="關閉交易設定" onClick={() => setSettingsOpen(false)}>×</button>
        </header>
        <div className="trade-settings-grid">
          <label><span>商品</span><select value={selection.symbol} onChange={event => setSelection(current => ({ ...current, symbol: event.target.value as SymbolKey }))}>{PRODUCT_OPTIONS.map(option => <option key={option.key} value={option.key}>{option.key} · {option.name}</option>)}</select></label>
          <label><span>K 棒週期</span><select value={selectedInterval} onChange={event => setSelection(current => ({ ...current, interval: event.target.value as Timeframe }))}>{TIMEFRAME_OPTIONS.map(option => <option key={option.key} value={option.key}>{option.name}</option>)}</select></label>
        </div>
        <fieldset className="trade-settings-strategies">
          <legend>交易策略 · 已啟用 {selectedStrategies.length} 套</legend>
          {strategyOptions.map(option => <label key={option.key}>
            <input type="checkbox" checked={selectedStrategies.includes(option.key)} onChange={() => toggleStrategy(option.key)} />
            <i style={{ background: option.color }} />
            <span><b>{option.name}</b><small>{option.category} · {option.description}</small></span>
          </label>)}
        </fieldset>
        <div className="trade-settings-status">
          <div className="paper-mode-pill">PAPER</div>
          <div className={`freshness-pill ${quoteFresh ? "fresh" : "stale"}`}>報價 {quoteAgeSeconds == null ? "等待中" : `${quoteAgeSeconds} 秒前`}</div>
          <div className={`connection-pill ${status}`}><i />{status === "connected" ? "即時連線" : status === "reconnecting" ? "重新連線中" : status === "connecting" ? "連線中" : "行情中斷"}</div>
        </div>
        <button className="trade-settings-done" type="button" onClick={() => setSettingsOpen(false)}>完成</button>
      </section>
    </>}
    <SystemNav active="/trade/" />
    <section className="live-summary">
      <div><span>商品／契約</span><b>{selection.symbol} · {latest?.contract ?? "等待行情"}</b></div>
      <div><span>交易時段</span><b>{latest?.session === "night" ? "夜盤" : latest?.session === "day" ? "日盤" : "—"}</b></div>
      <div><span>最後行情時間</span><b>{formatTaipeiDateTime(lastTick, "尚未收到")}</b></div>
      <div><span>資料延遲</span><b className={latency != null && latency > 1000 ? "warn" : ""}>{formatPrice(latency)} ms</b></div>
      <div><span>歷史 K 棒</span><b>{historyCount} 根</b></div>
    </section>
    <section className="strategy-strip">
      <div><span>策略圖層</span><b>{selectedStrategies.length ? `${selectedStrategies.length} 套啟用` : "全部隱藏"}</b></div>
      {strategyResults.map(strategy => <StrategyStatus key={strategy.key} strategy={strategy} />)}
      <small>僅用已收盤 K 棒確認；訊號於下一根開盤成立</small>
    </section>
    <PaperTradingDashboard
      quote={latest}
      quoteFresh={quoteFresh}
      marketHealth={marketHealth}
      onOverlayChange={updatePaperOverlay}
      strategyOptions={strategyOptions}
      strategyResults={strategyResults}
      selectedStrategies={selectedStrategies}
      symbol={selection.symbol}
      interval={selectedInterval}
      onStrategySelected={strategy => setSelection(current => ({
        ...current,
        strategies: [strategy],
      }))}
      marketPanel={<>
        <section className="live-chart-panel">
          <div className="live-toolbar">
            <div><strong>{latest?.contract ?? selection.symbol}</strong><span>{TIMEFRAME_OPTIONS.find(item => item.key === selectedInterval)?.name} · Asia/Taipei · Exchange Time</span></div>
            <div className="ohlc-strip"><span>O <b>{formatPrice(shown?.open)}</b></span><span>H <b>{formatPrice(shown?.high)}</b></span><span>L <b>{formatPrice(shown?.low)}</b></span><span>C <b>{formatPrice(shown?.close)}</b></span><span>V <b>{formatPrice(latest?.volume)}</b></span></div>
            <div className={`bar-state ${latest?.status ?? "forming"}`}>{latest?.status === "closed" ? "已收盤" : "形成中"}</div>
          </div>
          <div ref={hostRef} className="live-chart" />
          <div className="chart-legend"><span><i className="legend-forming" />形成中 K 棒</span><span><i className="legend-closed" />已收盤 K 棒</span>{paperOverlay.fills.some(fill => fill.order_source !== "strategy_auto") && <span><i className="legend-manual-fill" />Manual Paper Fill</span>}{paperOverlay.fills.some(fill => fill.order_source === "strategy_auto") && <span><i className="legend-auto-fill" />Auto Entry / Exit</span>}{paperOverlay.positions.length > 0 && <><span><i className="legend-position" />持倉均價</span><span><i className="legend-stop-line" />Stop Loss</span><span><i className="legend-take-line" />Take Profit</span></>}{strategyOptions.filter(option => selectedStrategies.includes(option.key)).map(option => <span key={option.key}><i style={{ background: option.color }} />{option.name}</span>)}</div>
        </section>
        {error && <div className="live-error">{error}；系統將以指數退避自動重連。</div>}
      </>}
    />
    <LiveShadowPanel />
    <LiveCanaryPanel />
    <LiveAutoPanel />
  </main>;
}
