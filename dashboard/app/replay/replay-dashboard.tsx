"use client";

import { type FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { apiRequest, apiUrl, jsonRequest, responseBody } from "../lib/api-client";
import { formatPrice, formatSignedMoney, formatTaipeiClock } from "../lib/formatters";
import type {
  ReplayOptions,
  ReplayOrder,
  ReplayPosition,
  ReplaySnapshot,
  ReplayTradingState,
  Session,
} from "./types";
import { useReplayChart } from "./use-replay-chart";
import { StrategyParameterSummary, StrategySeriesLegend } from "../backtest/strategy-diagnostics";

export type {
  ReplayBar,
  ReplaySignal,
  ReplaySnapshot,
  ReplayStrategy,
  ReplayTradingState,
  Session,
} from "./types";

import { initialStrategyKeys } from "../lib/strategy-catalog";

export default function ReplayDashboard() {
  const replaySessionRef = useRef<string | null>(null);
  const cursorSyncRef = useRef<Promise<boolean>>(Promise.resolve(true));
  const [options, setOptions] = useState<ReplayOptions | null>(null);
  const [date, setDate] = useState("");
  const [session, setSession] = useState<Session>("day");
  const [interval, setIntervalValue] = useState("1m");
  const [selected, setSelected] = useState<string[]>([]);
  const [snapshot, setSnapshot] = useState<ReplaySnapshot | null>(null);
  const [cursor, setCursor] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(2);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [trading, setTrading] = useState<ReplayTradingState | null>(null);
  const [tradeSide, setTradeSide] = useState<"buy" | "sell">("buy");
  const [tradeQuantity, setTradeQuantity] = useState(1);
  const [tradeStop, setTradeStop] = useState("");
  const [tradeTab, setTradeTab] = useState<"positions" | "orders" | "fills">("positions");
  const [tradeBusy, setTradeBusy] = useState("");
  const [tradeNotice, setTradeNotice] = useState("");
  const [diagnosticsVisible, setDiagnosticsVisible] = useState(true);

  useEffect(() => {
    let active = true;
    apiRequest<ReplayOptions>(
      "/api/replay/options?symbol=TMF",
      { cache: "no-store" },
      "回放選項載入失敗",
    )
      .then(body => {
        if (!active) return;
        const value = body;
        setOptions(value);
        setSelected(initialStrategyKeys(value.strategies));
        const latest = value.available_dates[value.available_dates.length - 1];
        setDate(latest?.date ?? value.available_end ?? "");
        setSession(latest?.sessions.some(item => item.key === "day") ? "day" : (latest?.sessions[0]?.key ?? "day"));
        setLoading(false);
      })
      .catch(reason => { if (active) { setError(reason instanceof Error ? reason.message : "回放選項載入失敗"); setLoading(false); } });
    return () => { active = false; };
  }, []);

  const availability = useMemo(() => options?.available_dates.find(item => item.date === date), [date, options]);
  const chooseDate = (nextDate: string) => {
    setDate(nextDate);
    const next = options?.available_dates.find(item => item.date === nextDate);
    if (next && !next.sessions.some(item => item.key === session)) setSession(next.sessions[0]?.key ?? "day");
  };

  const { hostRef, hoveredSignal } = useReplayChart({ snapshot, trading, cursor, diagnosticsVisible });
  useEffect(() => {
    if (!playing || !snapshot) return;
    const timer = window.setInterval(() => setCursor(value => { if (value >= snapshot.bars.length - 1) { setPlaying(false); return value; } return value + 1; }), Math.max(50, 1000 / speed));
    return () => window.clearInterval(timer);
  }, [playing, snapshot, speed]);

  const prepare = async () => {
    if (!date || !selected.length) return;
    setLoading(true); setError(""); setTradeNotice(""); setPlaying(false);
    try {
      const prepared = await apiRequest<ReplaySnapshot>(
        "/api/replay/prepare",
        jsonRequest("POST", {
          symbol: "TMF",
          trading_date: date,
          session,
          interval,
          strategies: selected,
        }),
        "回放快照建立失敗",
      );
      replaySessionRef.current = prepared.trading_session.session_id;
      cursorSyncRef.current = Promise.resolve(true);
      setSnapshot(prepared); setTrading(prepared.trading_session); setCursor(0);
      setTradeTab("positions"); setTradeSide("buy"); setTradeQuantity(1);
      setTradeStop(String(prepared.bars[0].close - 50));
    } catch (reason) { setError(reason instanceof Error ? reason.message : "回放快照建立失敗"); }
    finally { setLoading(false); }
  };

  const toggleStrategy = (key: string) => setSelected(current => current.includes(key) ? current.filter(item => item !== key) : current.length < (options?.max_strategies ?? 3) ? [...current, key] : current);
  const current = snapshot?.bars[cursor];
  const progress = snapshot?.bars.length ? Math.round((cursor + 1) / snapshot.bars.length * 100) : 0;
  const visibleSignals = snapshot?.strategies.map(strategy => ({ ...strategy, visible: strategy.signals.filter(signal => !current || Date.parse(signal.time) <= Date.parse(current.end_time)) })) ?? [];

  const syncCursor = useCallback((nextCursor: number) => {
    const requestedSession = replaySessionRef.current;
    if (!requestedSession) return Promise.resolve(false);
    const task = cursorSyncRef.current.catch(() => false).then(async () => {
      if (replaySessionRef.current !== requestedSession) return false;
      const body = await apiRequest<ReplayTradingState>(
        `/api/replay/sessions/${requestedSession}/cursor`,
        jsonRequest("PUT", { cursor: nextCursor }),
        "回放交易時間同步失敗",
      );
      if (replaySessionRef.current !== requestedSession) return false;
      const state = body as ReplayTradingState;
      setTrading(state);
      if (state.rewound) setTradeNotice("時間軸已倒退，Replay 帳戶與所有模擬成交已重設。");
      return true;
    }).catch(reason => {
      setPlaying(false);
      setError(reason instanceof Error ? reason.message : "回放交易時間同步失敗");
      return false;
    });
    cursorSyncRef.current = task;
    return task;
  }, []);

  useEffect(() => {
    if (snapshot?.trading_session.session_id) void syncCursor(cursor);
  }, [cursor, snapshot?.trading_session.session_id, syncCursor]);

  const chooseTradeSide = (nextSide: "buy" | "sell") => {
    setTradeSide(nextSide);
    if (current) setTradeStop(String(current.close + (nextSide === "buy" ? -50 : 50)));
  };

  const submitReplayOrder = async (event: FormEvent) => {
    event.preventDefault();
    const sessionId = replaySessionRef.current;
    if (!sessionId || !current) return;
    setPlaying(false); setTradeBusy("order"); setError(""); setTradeNotice("");
    try {
      if (!await syncCursor(cursor)) throw new Error("回放交易時間尚未同步");
      const response = await fetch(apiUrl(`/api/replay/sessions/${sessionId}/orders`), {
        method: "POST",
        headers: { "Content-Type": "application/json", "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify({ strategy_id: "manual-replay", strategy_version: 1, side: tradeSide, quantity: tradeQuantity, stop_loss_price: Number(tradeStop) }),
      });
      const body = await responseBody<{ session: ReplayTradingState; order: ReplayOrder }>(
        response,
        "Replay 模擬委託失敗",
      );
      setTrading(body.session as ReplayTradingState);
      setTradeNotice(body.order.status === "filled" ? `Replay ${tradeSide === "buy" ? "買進" : "賣出"} ${tradeQuantity} 口已成交` : `委託未成交：${body.order.status_reason}`);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Replay 模擬委託失敗"); }
    finally { setTradeBusy(""); }
  };

  const closeReplayPosition = async (position: ReplayPosition) => {
    const sessionId = replaySessionRef.current;
    if (!sessionId || !current || !window.confirm(`確定以 Replay 目前價格平倉 ${position.contract} ${Math.abs(position.quantity)} 口？`)) return;
    setPlaying(false); setTradeBusy(`close:${position.contract}`); setError(""); setTradeNotice("");
    try {
      if (!await syncCursor(cursor)) throw new Error("回放交易時間尚未同步");
      const response = await fetch(apiUrl(`/api/replay/sessions/${sessionId}/orders`), {
        method: "POST",
        headers: { "Content-Type": "application/json", "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify({ strategy_id: position.strategy_id, strategy_version: position.strategy_version, side: position.quantity > 0 ? "sell" : "buy", quantity: Math.abs(position.quantity), reduce_only: true }),
      });
      const body = await responseBody<{ session: ReplayTradingState }>(response, "Replay 平倉失敗");
      setTrading(body.session as ReplayTradingState); setTradeNotice(`${position.contract} 已完成 Replay 平倉`);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Replay 平倉失敗"); }
    finally { setTradeBusy(""); }
  };

  const resetReplayTrading = async () => {
    const sessionId = replaySessionRef.current;
    if (!sessionId || !window.confirm("確定清除這次 Replay 的持倉、委託與成交？")) return;
    setPlaying(false); setTradeBusy("reset"); setError("");
    try {
      const body = await apiRequest<ReplayTradingState>(
        `/api/replay/sessions/${sessionId}/reset`,
        { method: "POST" },
        "Replay 帳戶重設失敗",
      );
      cursorSyncRef.current = Promise.resolve(true); setCursor(0); setTrading(body as ReplayTradingState);
      setTradeNotice("Replay 帳戶已重設，正式 Paper 帳戶不受影響。");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Replay 帳戶重設失敗"); }
    finally { setTradeBusy(""); }
  };

  return <>
    <section className="replay-config panel">
      <div className="replay-config-grid">
        <label><span>交易日</span><select value={date} onChange={event => chooseDate(event.target.value)} disabled={loading}>{options?.available_dates.slice().reverse().map(item => <option key={item.date} value={item.date}>{item.date}</option>)}</select></label>
        <label><span>交易時段</span><select value={session} onChange={event => setSession(event.target.value as Session)} disabled={loading}>{options?.sessions.filter(item => availability?.sessions.some(value => value.key === item.key)).map(item => <option key={item.key} value={item.key}>{item.name}</option>)}</select></label>
        <label><span>K 棒週期</span><select value={interval} onChange={event => setIntervalValue(event.target.value)} disabled={loading}>{options?.intervals.map(item => <option key={item.key} value={item.key}>{item.name}</option>)}</select></label>
        <button onClick={prepare} disabled={loading || !date || !selected.length}>{loading ? "準備中…" : "建立回放"}</button>
      </div>
      <div className="replay-strategy-picker"><header><span>疊加策略（最多 {options?.max_strategies ?? 3} 個）</span><small>訊號只會在時間軸抵達後出現</small></header><div>{options?.strategies.map(item => <button key={item.key} type="button" className={selected.includes(item.key) ? "active" : ""} onClick={() => toggleStrategy(item.key)} disabled={!selected.includes(item.key) && selected.length >= (options?.max_strategies ?? 3)}><i style={{ background: item.color }} />{item.kind === "composite" ? "組合 · " : ""}{item.name}</button>)}</div></div>
    </section>
    {error && <div className="live-error">{error}</div>}
    {!snapshot && !loading && !error && <section className="replay-empty panel"><b>選擇一個歷史盤次開始回放</b><p>建立快照後可播放、暫停、調速、逐根前進或拖曳時間軸。</p></section>}
    {snapshot && <>
      <section className="replay-player panel">
        <div className="replay-player-head"><div><span>REPLAY SNAPSHOT · {snapshot.snapshot_id.slice(0, 8).toUpperCase()}</span><h2>{snapshot.symbol} · {snapshot.trading_date} · {snapshot.session === "day" ? "日盤" : "夜盤"}</h2></div><div className="replay-quote"><small>{formatTaipeiClock(current?.time)} · {snapshot.interval_name}</small><strong>{formatPrice(current?.close)}</strong><em>{progress}%</em></div></div>
        <StrategyParameterSummary visualizations={snapshot.strategies.flatMap(strategy => strategy.visualization ? [strategy.visualization] : [])} />
        <StrategySeriesLegend visualizations={snapshot.strategies.flatMap(strategy => strategy.visualization ? [strategy.visualization] : [])} />
        {snapshot.strategies.some(strategy => strategy.visualization?.diagnostics.length) && <div className="replay-diagnostic-toggle"><button type="button" onClick={() => setDiagnosticsVisible(value => !value)}>{diagnosticsVisible ? "收合策略診斷" : "展開策略診斷"}</button></div>}
        <div className="replay-chart-stage"><div className="replay-chart" ref={hostRef} />{hoveredSignal && <aside className="strategy-trigger-tooltip"><b>{hoveredSignal.strategyName} · {hoveredSignal.signal.event.toUpperCase()} · {hoveredSignal.signal.direction.toUpperCase()}</b><span>{hoveredSignal.signal.trigger_reason ?? hoveredSignal.signal.reason}</span>{hoveredSignal.signal.event === "entry" && <><span>訊號確認 {formatTaipeiClock(hoveredSignal.signal.trigger_time ?? hoveredSignal.signal.time)}</span><span>成交 {formatTaipeiClock(hoveredSignal.signal.time)}</span></>}<span>價格 {formatPrice(hoveredSignal.signal.price)}</span><span>停損 {formatPrice(hoveredSignal.signal.stop_loss_price)} · 停利 {formatPrice(hoveredSignal.signal.take_profit_price)}</span>{Object.entries(hoveredSignal.signal.context ?? {}).slice(0, 5).map(([key, value]) => <span key={key}>{key} = {Number(value).toFixed(4)}</span>)}</aside>}</div>
        <div className="replay-controls"><button onClick={() => { setPlaying(false); setCursor(0); }} aria-label="回到開頭">↺</button><button onClick={() => { setPlaying(false); setCursor(value => Math.max(0, value - 1)); }} aria-label="上一根">｜◀</button><button className="play" onClick={() => { if (cursor >= snapshot.bars.length - 1) setCursor(0); setPlaying(value => !value); }}>{playing ? "暫停" : "播放"}</button><button onClick={() => { setPlaying(false); setCursor(value => Math.min(snapshot.bars.length - 1, value + 1)); }} aria-label="下一根">▶｜</button><label><span>速度</span><select value={speed} onChange={event => setSpeed(Number(event.target.value))}>{[0.5, 1, 2, 5, 10].map(value => <option key={value} value={value}>{value}×</option>)}</select></label><input aria-label="回放進度" type="range" min={0} max={Math.max(0, snapshot.bars.length - 1)} value={cursor} onChange={event => { setPlaying(false); setCursor(Number(event.target.value)); }} /><small>{cursor + 1} / {snapshot.bars.length} 根</small></div>
      </section>
      <section className="replay-trading panel">
        <header className="replay-trading-head"><div><span>ISOLATED REPLAY TRADING</span><h2>歷史情境模擬下單</h2><p>只使用目前回放時間與價格；帳戶、持倉、委託及成交皆不會寫入正式 Paper。</p></div><div><b>REPLAY MODE</b><small>虛擬時間 {formatTaipeiClock(trading?.virtual_time)}</small></div></header>
        {tradeNotice && <div className="replay-trade-notice">{tradeNotice}</div>}
        <div className="replay-trade-metrics">
          <article><span>未實現損益</span><strong className={(trading?.positions.reduce((sum, item) => sum + item.unrealized_pnl, 0) ?? 0) >= 0 ? "profit" : "loss"}>{formatSignedMoney(trading?.positions.reduce((sum, item) => sum + item.unrealized_pnl, 0) ?? 0)}</strong></article>
          <article><span>已實現損益</span><strong className={(trading?.account.realized_pnl ?? 0) >= 0 ? "profit" : "loss"}>{formatSignedMoney(trading?.account.realized_pnl ?? 0)}</strong></article>
          <article><span>目前持倉</span><strong>{trading?.account.open_contracts ?? 0} 口</strong></article>
          <article><span>Replay 成交</span><strong>{trading?.account.trades ?? 0} 筆</strong></article>
        </div>
        <div className="replay-trade-grid">
          <form className="replay-order-ticket" onSubmit={submitReplayOrder}>
            <div><span>目前模擬價格</span><strong>{current?.contract ?? "—"} · {formatPrice(current?.close)}</strong></div>
            <div className="replay-order-side"><button type="button" className={tradeSide === "buy" ? "buy active" : "buy"} onClick={() => chooseTradeSide("buy")}>買進／做多</button><button type="button" className={tradeSide === "sell" ? "sell active" : "sell"} onClick={() => chooseTradeSide("sell")}>賣出／做空</button></div>
            <label><span>數量</span><select value={tradeQuantity} onChange={event => setTradeQuantity(Number(event.target.value))}><option value={1}>1 口</option><option value={2}>2 口</option></select></label>
            <label><span>停損價</span><input required min="1" step="1" inputMode="decimal" value={tradeStop} onChange={event => setTradeStop(event.target.value)} /></label>
            <button className={`replay-order-submit ${tradeSide}`} disabled={!current || Boolean(tradeBusy)}>{tradeBusy === "order" ? "送單中…" : `以 ${formatPrice(current?.close)} 模擬${tradeSide === "buy" ? "買進" : "賣出"}`}</button>
            <small>成交價與時間由伺服器依 Replay 游標決定，仍套用相同成本、持倉上限與停損風控。</small>
          </form>
          <div className="replay-trade-ledger">
            <div className="replay-trade-tabs" role="tablist" aria-label="Replay 交易紀錄">
              <button type="button" className={tradeTab === "positions" ? "active" : ""} onClick={() => setTradeTab("positions")}>持倉 <b>{trading?.positions.length ?? 0}</b></button>
              <button type="button" className={tradeTab === "orders" ? "active" : ""} onClick={() => setTradeTab("orders")}>委託 <b>{trading?.orders.length ?? 0}</b></button>
              <button type="button" className={tradeTab === "fills" ? "active" : ""} onClick={() => setTradeTab("fills")}>成交 <b>{trading?.fills.length ?? 0}</b></button>
            </div>
            <div className="replay-trade-list">
              {tradeTab === "positions" && <>{trading?.positions.map(position => <article key={`${position.strategy_id}:${position.contract}`}><div><b>{position.contract}</b><span className={position.quantity > 0 ? "profit" : "loss"}>{position.quantity > 0 ? "多" : "空"} {Math.abs(position.quantity)} 口</span></div><strong>均價 {formatPrice(position.average_price)} · {formatSignedMoney(position.unrealized_pnl)}</strong><button type="button" disabled={Boolean(tradeBusy)} onClick={() => void closeReplayPosition(position)}>{tradeBusy === `close:${position.contract}` ? "平倉中…" : "全部平倉"}</button></article>)}{!trading?.positions.length && <p>目前沒有 Replay 持倉。</p>}</>}
              {tradeTab === "orders" && <>{trading?.orders.slice(0, 20).map(order => <article key={order.order_id}><div><b>{order.side === "buy" ? "買進" : "賣出"} {order.quantity} 口</b><span>{order.status === "filled" ? "已成交" : order.status === "rejected" ? "已拒絕" : "處理中"}</span></div><strong>{order.contract} · {formatPrice(order.reference_price)}</strong><small>{formatTaipeiClock(order.submitted_at)} · {order.status_reason}</small></article>)}{!trading?.orders.length && <p>尚無 Replay 委託。</p>}</>}
              {tradeTab === "fills" && <>{trading?.fills.slice(0, 20).map(fill => <article key={fill.fill_id}><div><b>{fill.side === "buy" ? "買進" : "賣出"} {fill.quantity} 口</b><span>已成交</span></div><strong>{fill.contract} · {formatPrice(fill.price)}</strong><small>{formatTaipeiClock(fill.meta.occurred_at)} · 成本 {formatSignedMoney(-(fill.commission + fill.tax))}</small></article>)}{!trading?.fills.length && <p>尚無 Replay 成交。</p>}</>}
            </div>
          </div>
        </div>
        <footer className="replay-trade-footer"><span>時間軸倒退會自動清除 Replay 交易，避免使用未來資訊。</span><button type="button" disabled={Boolean(tradeBusy)} onClick={() => void resetReplayTrading()}>{tradeBusy === "reset" ? "重設中…" : "重設 Replay 帳戶"}</button></footer>
      </section>
      <section className="replay-lower">
        <div className="replay-strategy-status panel"><header><span>STRATEGY EVENTS</span><h2>截至目前的策略訊號</h2></header>{visibleSignals.map(strategy => { const latest = strategy.visible[strategy.visible.length - 1]; return <article key={strategy.key}><div><i style={{ background: strategy.color }} /><strong>{strategy.name}</strong><small>{strategy.visible.length} 個訊號</small></div>{latest ? <dl><div><dt>狀態</dt><dd>{latest.event === "entry" ? latest.direction === "long" ? "多單進場" : "空單進場" : "已出場"}</dd></div><div><dt>時間</dt><dd>{formatTaipeiClock(latest.time)}</dd></div><div><dt>價格</dt><dd>{formatPrice(latest.price)}</dd></div><div><dt>停損 / 停利</dt><dd>{formatPrice(latest.stop_loss_price)} / {formatPrice(latest.take_profit_price)}</dd></div></dl> : <p>時間軸尚未出現訊號</p>}</article>; })}</div>
        <aside className="replay-now panel"><span>NOW PLAYING</span><h2>{formatTaipeiClock(current?.time)}</h2><dl><div><dt>開</dt><dd>{formatPrice(current?.open)}</dd></div><div><dt>高</dt><dd>{formatPrice(current?.high)}</dd></div><div><dt>低</dt><dd>{formatPrice(current?.low)}</dd></div><div><dt>收</dt><dd>{formatPrice(current?.close)}</dd></div><div><dt>量</dt><dd>{formatPrice(current?.volume)}</dd></div><div><dt>合約</dt><dd>{current?.contract ?? "—"}</dd></div></dl><p>這是使用者專屬的歷史快照；回放交易具有獨立虛擬帳戶，不會影響即時行情或正式 Paper。</p></aside>
      </section>
    </>}
  </>;
}
