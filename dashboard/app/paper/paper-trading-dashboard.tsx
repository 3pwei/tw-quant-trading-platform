"use client";

import Link from "next/link";
import { FormEvent, type ReactNode, useEffect, useMemo, useState } from "react";
import type { StrategyOption, StrategyResult, Timeframe } from "../live/types";
import { apiRequest, jsonRequest } from "../lib/api-client";
import {
  formatMoney,
  formatPrice,
  formatSignedMoney,
  formatTaipeiDateTime,
} from "../lib/formatters";
import type {
  MarketHealth,
  PaperOrder,
  PaperOverlaySnapshot,
  PaperPosition,
  PaperQuote,
} from "./types";
import { usePaperAccount } from "./use-paper-account";
import PaperAutoPanel from "../trade/paper-auto-panel";
import type { WorkspaceTradingMode } from "../trade/paper-auto-types";

export type {
  Account,
  CurrentUser,
  MarketHealth,
  PaperFill,
  PaperOrder,
  PaperOverlaySnapshot,
  PaperPosition,
  PaperQuote,
} from "./types";

type PaperTradingDashboardProps = {
  marketPanel: ReactNode;
  quote: PaperQuote | null;
  quoteFresh: boolean;
  marketHealth: MarketHealth | null;
  onOverlayChange: (snapshot: PaperOverlaySnapshot) => void;
  strategyOptions: StrategyOption[];
  strategyResults: StrategyResult[];
  selectedStrategies: string[];
  symbol: string;
  interval: Timeframe;
  onStrategySelected: (strategy: string) => void;
};

type LedgerTab = "positions" | "orders" | "fills";

const reasonLabels: Record<string, string> = {
  approved: "風控通過",
  simulated_fill: "模擬成交",
  kill_switch_active: "Kill Switch 已啟用",
  max_position_exceeded: "超過最大持倉",
  max_trade_risk_exceeded: "超過單筆風險",
  stop_loss_required: "缺少停損",
  invalid_stop_direction: "停損方向錯誤",
  paper_mode_required: "尚未啟用 Paper",
  risk_reducing_approved: "減倉核准",
};
const reasonLabel = (value: string) => reasonLabels[value] ?? value;
const sourceLabel = (source?: "manual" | "strategy_auto") => (
  source === "strategy_auto" ? "策略自動" : "手動"
);

export default function PaperTradingDashboard({
  marketPanel,
  quote,
  quoteFresh,
  marketHealth,
  onOverlayChange,
  strategyOptions,
  strategyResults,
  selectedStrategies,
  symbol,
  interval,
  onStrategySelected,
}: PaperTradingDashboardProps) {
  const {
    user, account, positions, orders, fills, error, setError, loading, load,
  } = usePaperAccount(onOverlayChange);
  const [side, setSide] = useState<"buy" | "sell">("buy");
  const [quantity, setQuantity] = useState(1);
  const [stopLoss, setStopLoss] = useState<string | null>(null);
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState("");
  const [ledgerTab, setLedgerTab] = useState<LedgerTab>("positions");
  const [mobileOrderOpen, setMobileOrderOpen] = useState(false);
  const [workspaceMode, setWorkspaceMode] = useState<WorkspaceTradingMode>("observe");

  useEffect(() => {
    if (!mobileOrderOpen) return;
    const previousOverflow = document.body.style.overflow;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMobileOrderOpen(false);
    };
    document.body.style.overflow = "hidden";
    window.addEventListener("keydown", closeOnEscape);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", closeOnEscape);
    };
  }, [mobileOrderOpen]);

  const totalUnrealized = useMemo(
    () => positions.reduce((sum, position) => sum + position.unrealized_pnl, 0),
    [positions],
  );
  const paperEnabled = user?.trading_mode === "paper" && user.permissions.includes("orders.paper");
  const orderStopLoss = stopLoss ?? (quote ? String(quote.close + (side === "buy" ? -50 : 50)) : "");
  const marketBlockReason = marketHealth?.trading_block_reason ?? null;
  const marketBlockCopy = marketBlockReason === "provider_disconnected"
    ? { code: "PROVIDER DISCONNECTED", title: "行情供應商連線中斷", detail: "系統已禁止建立新倉；既有持倉仍可查看。連線恢復後也不會自動補送中斷期間的委託。" }
    : marketBlockReason === "market_stale"
      ? { code: "MARKET STALE", title: "行情已停止更新", detail: `最新 Tick：${formatTaipeiDateTime(marketHealth?.last_tick_time)}。系統已禁止建立新倉，恢復後請重新確認價格再送單。` }
      : null;
  const orderDisabled = !paperEnabled || !quoteFresh || Boolean(marketBlockReason)
    || Boolean(account?.kill_switch_active) || Boolean(busy);
  const ledgerTabs: { key: LedgerTab; label: string; count: number }[] = [
    { key: "positions", label: "目前持倉", count: positions.length },
    { key: "orders", label: "最近委託", count: orders.length },
    { key: "fills", label: "最近成交", count: fills.length },
  ];
  const latestObservedSignal = strategyResults
    .flatMap(strategy => strategy.signals.map(signal => ({ ...signal, strategyName: strategy.name })))
    .sort((left, right) => Date.parse(right.time) - Date.parse(left.time))[0];

  function chooseSide(nextSide: "buy" | "sell") {
    setSide(nextSide);
    if (quote) setStopLoss(String(quote.close + (nextSide === "buy" ? -50 : 50)));
  }

  function openMobileOrder(nextSide: "buy" | "sell") {
    chooseSide(nextSide);
    setMobileOrderOpen(true);
  }

  async function submitOrder(event: FormEvent) {
    event.preventDefault();
    setBusy("order"); setError(""); setNotice("");
    try {
      const body = await apiRequest<{ order: PaperOrder }>("/api/paper/orders", {
        ...jsonRequest("POST", { side, quantity, stop_loss_price: Number(orderStopLoss) }),
        headers: { "Content-Type": "application/json", "Idempotency-Key": crypto.randomUUID() },
      }, "API 錯誤");
      setNotice(body.order.status === "filled"
        ? `模擬${side === "buy" ? "買進" : "賣出"} ${quantity} 口已成交`
        : `委託未成交：${reasonLabel(body.order.status_reason)}`);
      await load(true);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "模擬委託失敗");
    } finally { setBusy(""); }
  }

  async function closePosition(position: PaperPosition) {
    if (!window.confirm(`確定以最新行情平倉 ${position.contract} ${Math.abs(position.quantity)} 口？`)) return;
    setBusy(`close:${position.contract}`); setError(""); setNotice("");
    try {
      const body = await apiRequest<{ order: PaperOrder }>("/api/paper/orders", {
        ...jsonRequest("POST", {
          strategy_id: position.strategy_id, strategy_version: position.strategy_version,
          side: position.quantity > 0 ? "sell" : "buy",
          quantity: Math.abs(position.quantity), reduce_only: true,
        }),
        headers: { "Content-Type": "application/json", "Idempotency-Key": crypto.randomUUID() },
      }, "API 錯誤");
      if (body.order.status !== "filled") throw new Error(reasonLabel(body.order.status_reason));
      setNotice(`${position.contract} 已完成模擬平倉`); await load(true);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "平倉失敗");
    } finally { setBusy(""); }
  }

  async function control(action: "activate" | "reset") {
    if (action === "activate" && !window.confirm("啟用 Kill Switch 後將阻止所有新增曝險，確定繼續？")) return;
    setBusy("control"); setError(""); setNotice("");
    try {
      const endpoint = action === "activate" ? "/api/paper/kill-switch" : "/api/paper/kill-switch/reset";
      await apiRequest(endpoint, jsonRequest("POST", {
        reason: action === "activate" ? "manual_ui_stop" : "manual_ui_resume",
      }), "API 錯誤");
      setNotice(action === "activate" ? "Kill Switch 已啟用" : "Kill Switch 已解除");
      await load(true);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "控制操作失敗");
    } finally { setBusy(""); }
  }

  return <div className="paper-page">
    {loading && <section className="panel paper-loading">正在讀取模擬帳戶…</section>}
    {!loading && !paperEnabled && <section className="paper-mode-warning panel">
      <div><span>PAPER MODE DISABLED</span><h2>帳號尚未啟用模擬下單</h2><p>可以查看帳戶狀態，但送單按鈕維持停用。</p></div>
      {user?.role === "admin" && <Link href="/admin/users/">前往帳號權限啟用 Paper →</Link>}
    </section>}
    {account?.recovery_status === "degraded" && <section className="paper-mode-warning panel">
      <div><span>RECOVERY LOCK</span><h2>帳戶狀態需要檢查</h2><p>重啟復原發現資料不一致，系統已禁止新增曝險；既有部位仍可平倉。</p></div>
      {user?.role === "admin" && <Link href="/settings/">查看系統健康狀態 →</Link>}
    </section>}
    {marketBlockCopy && <section className="paper-mode-warning market-interruption panel">
      <div><span>{marketBlockCopy.code}</span><h2>{marketBlockCopy.title}</h2><p>{marketBlockCopy.detail}</p></div>
      {user?.role === "admin" && <Link href="/settings/">查看系統健康狀態 →</Link>}
    </section>}
    {error && <div className="paper-message error">{error}</div>}
    {notice && <div className="paper-message success">{notice}</div>}

    <section className="paper-metrics">
      <article><span>最新模擬報價</span><strong>{formatPrice(quote?.close)}</strong><small>{quote?.contract ?? "等待行情"} · {quoteFresh ? "即時" : "已過期"}</small></article>
      <article><span>未實現損益</span><strong className={totalUnrealized >= 0 ? "profit" : "loss"}>{formatSignedMoney(totalUnrealized)}</strong><small>{account?.open_contracts ?? 0} 口未平倉</small></article>
      <article><span>已實現損益</span><strong className={(account?.realized_pnl ?? 0) >= 0 ? "profit" : "loss"}>{formatSignedMoney(account?.realized_pnl ?? 0)}</strong><small>今日成交 {account?.trades ?? 0} 筆</small></article>
      <article><span>風控狀態</span><strong className={!paperEnabled ? "warning" : account?.kill_switch_active ? "loss" : "profit"}>{!paperEnabled ? "未啟用" : account?.kill_switch_active ? "已停止" : "可交易"}</strong><small>{account?.kill_switch_reason ? reasonLabel(account.kill_switch_reason) : paperEnabled ? "風控閘門正常" : "送單功能維持停用"}</small></article>
    </section>

    <nav className="workspace-mode-tabs panel" aria-label="交易模式">
      <button type="button" className={workspaceMode === "observe" ? "active" : ""} aria-pressed={workspaceMode === "observe"} onClick={() => setWorkspaceMode("observe")}><b>OBSERVE</b><small>只看策略，不下單</small></button>
      <button type="button" className={workspaceMode === "manual_paper" ? "active" : ""} aria-pressed={workspaceMode === "manual_paper"} onClick={() => setWorkspaceMode("manual_paper")}><b>MANUAL PAPER</b><small>手動模擬委託</small></button>
      <button type="button" className={workspaceMode === "paper_auto" ? "active auto" : ""} aria-pressed={workspaceMode === "paper_auto"} onClick={() => setWorkspaceMode("paper_auto")}><b>PAPER AUTO</b><small>需確認並 ARM</small></button>
    </nav>

    <div className="trade-workspace-grid">
      <div className="trade-market-column">{marketPanel}</div>
      <aside className="trade-order-column">
      {workspaceMode === "observe" && <section className="observe-panel panel">
        <div className="panel-head"><div><span>OBSERVE</span><h2>策略觀察</h2></div><strong>NO ORDERS</strong></div>
        <p>只顯示 closed-bar 策略訊號與 overlays；此模式不建立任何委託。</p>
        <div className="observe-strategies">{strategyResults.map(strategy => <div key={strategy.key}><i style={{ background: strategy.color }} /><span><b>{strategy.name}</b><small>{strategy.signals.length} 個訊號</small></span></div>)}</div>
        <section className="observe-latest"><span>LATEST SIGNAL</span>{latestObservedSignal ? <><b>{latestObservedSignal.strategyName}</b><strong>{latestObservedSignal.event.toUpperCase()} · {latestObservedSignal.direction.toUpperCase()}</strong><p>{latestObservedSignal.reason}</p><small>{formatTaipeiDateTime(latestObservedSignal.time)}</small></> : <p>等待已收盤 K 棒產生策略訊號。</p>}</section>
      </section>}
      {workspaceMode === "manual_paper" && <>
      <form className={`paper-order panel ${mobileOrderOpen ? "mobile-open" : ""}`} onSubmit={submitOrder} aria-label="模擬市價單">
        <div className="panel-head"><div><span>MANUAL ORDER</span><h2>模擬市價單</h2></div><small>成交價由伺服器決定</small><button type="button" className="mobile-sheet-close" aria-label="關閉下單面板" onClick={() => setMobileOrderOpen(false)}>×</button></div>
        <div className="paper-side">
          <button type="button" className={side === "buy" ? "active buy" : ""} onClick={() => chooseSide("buy")}>買進／做多</button>
          <button type="button" className={side === "sell" ? "active sell" : ""} onClick={() => chooseSide("sell")}>賣出／做空</button>
        </div>
        <label><span>數量</span><select value={quantity} onChange={event => setQuantity(Number(event.target.value))}><option value={1}>1 口</option><option value={2}>2 口</option></select></label>
        <label><span>停損價</span><input required min="1" step="1" inputMode="decimal" value={orderStopLoss} onChange={event => setStopLoss(event.target.value)} /></label>
        <p>單筆風險與最大持倉仍由後端再次檢查。行情超過系統容許延遲時不會成交。</p>
        <button className={`paper-submit ${side}`} disabled={orderDisabled}>{busy === "order" ? "送單中…" : `送出模擬${side === "buy" ? "買單" : "賣單"}`}</button>
      </form>

      <section className="paper-risk panel">
        <div className="panel-head"><div><span>ACCOUNT CONTROL</span><h2>Kill Switch</h2></div><small>只阻止新增曝險</small></div>
        <strong className={account?.kill_switch_active ? "loss" : "profit"}>{account?.kill_switch_active ? "ACTIVE" : "READY"}</strong>
        <p>{account?.kill_switch_active ? `原因：${reasonLabel(account.kill_switch_reason ?? "manual")}` : "發生異常時可立即停止所有新的模擬進場；既有部位仍可平倉。"}</p>
        {account?.kill_switch_active
          ? <button className="reset" disabled={Boolean(busy)} onClick={() => void control("reset")}>解除 Kill Switch</button>
          : <button className="activate" disabled={Boolean(busy) || !paperEnabled} onClick={() => void control("activate")}>啟用 Kill Switch</button>}
      </section>
      </>}
      {workspaceMode === "paper_auto" && <PaperAutoPanel
        user={user}
        account={account}
        marketHealth={marketHealth}
        strategyOptions={strategyOptions}
        selectedStrategies={selectedStrategies}
        symbol={symbol}
        interval={interval}
        positions={positions}
        orders={orders}
        fills={fills}
        busy={Boolean(busy)}
        onClosePosition={closePosition}
        onKillSwitch={() => control("activate")}
        onStrategySelected={onStrategySelected}
      />}
      </aside>
    </div>

    <section className="paper-ledger panel">
      <div className="paper-ledger-tabs" role="tablist" aria-label="模擬交易紀錄">
        {ledgerTabs.map(tab => <button key={tab.key} id={`paper-tab-${tab.key}`} type="button" role="tab" aria-controls={`paper-panel-${tab.key}`} aria-selected={ledgerTab === tab.key} className={ledgerTab === tab.key ? "active" : ""} onClick={() => setLedgerTab(tab.key)}><span>{tab.label}</span><b>{tab.count}</b></button>)}
      </div>
      {ledgerTab === "positions" && <div id="paper-panel-positions" className="paper-positions" role="tabpanel" aria-labelledby="paper-tab-positions">
        <div className="table-scroll"><table><thead><tr><th>契約</th><th>方向／口數</th><th>均價</th><th>未實現損益</th><th>建立時間</th><th></th></tr></thead><tbody>
          {positions.map(position => <tr key={`${position.strategy_id}:${position.contract}:${position.runtime_id ?? "manual"}`}><td data-label="契約"><b>{position.contract}</b><small>{position.order_source === "strategy_auto" ? `AUTO · ${position.strategy_id} v${position.strategy_version}` : `${position.strategy_id} · v${position.strategy_version} · ${sourceLabel(position.order_source)}`}</small></td><td data-label="方向／口數"><i className={`dir ${position.quantity > 0 ? "long" : "short"}`}>{position.quantity > 0 ? "多" : "空"}</i> {Math.abs(position.quantity)} 口</td><td data-label="均價">{formatPrice(position.average_price)}</td><td data-label="未實現損益" className={position.unrealized_pnl >= 0 ? "profit" : "loss"}><b>{formatSignedMoney(position.unrealized_pnl)}</b></td><td data-label="建立時間">{formatTaipeiDateTime(position.opened_at)}</td><td className="paper-close-cell"><button disabled={Boolean(busy) || !quoteFresh} onClick={() => void closePosition(position)}>{busy === `close:${position.contract}` ? "平倉中…" : position.order_source === "strategy_auto" ? "手動緊急平倉" : "全部平倉"}</button></td></tr>)}
        </tbody></table>{!positions.length && <p className="paper-empty">目前沒有模擬持倉。</p>}</div>
      </div>}
      {ledgerTab === "orders" && <div id="paper-panel-orders" className="paper-record-list" role="tabpanel" aria-labelledby="paper-tab-orders">{orders.slice(0, 20).map(order => <article key={order.order_id}><div><b>{order.side === "buy" ? "買進" : "賣出"} {order.quantity} 口</b><span className={order.status}>{order.status === "filled" ? "已成交" : order.status === "rejected" ? "已拒絕" : "處理中"}</span></div><strong>{order.contract} · {formatPrice(order.reference_price)}</strong><small>{sourceLabel(order.order_source)} · {formatTaipeiDateTime(order.submitted_at)} · {reasonLabel(order.status_reason)}</small></article>)}{!orders.length && <p className="paper-empty">尚無委託紀錄。</p>}</div>}
      {ledgerTab === "fills" && <div id="paper-panel-fills" className="paper-record-list" role="tabpanel" aria-labelledby="paper-tab-fills">{fills.slice(0, 20).map(fill => <article key={fill.fill_id}><div><b>{fill.side === "buy" ? "買進" : "賣出"} {fill.quantity} 口</b><span className="filled">已成交</span></div><strong>{fill.contract} · {formatPrice(fill.price)}</strong><small>{sourceLabel(fill.order_source)} · {formatTaipeiDateTime(fill.meta.occurred_at)} · 成本 NT$ {formatMoney(fill.commission + fill.tax)} · 滑價 {formatPrice(fill.slippage)} 點</small></article>)}{!fills.length && <p className="paper-empty">尚無成交紀錄。</p>}</div>}
    </section>

    {workspaceMode === "manual_paper" && mobileOrderOpen && <button type="button" className="mobile-sheet-backdrop" aria-label="關閉下單面板" onClick={() => setMobileOrderOpen(false)} />}
    {workspaceMode === "manual_paper" && <div className="mobile-trade-bar" aria-label="快速模擬下單">
      <span><small>{quote?.contract ?? "等待行情"}</small><b>{formatPrice(quote?.close)}</b></span>
      <button type="button" className="buy" disabled={orderDisabled} onClick={() => openMobileOrder("buy")}>買進</button>
      <button type="button" className="sell" disabled={orderDisabled} onClick={() => openMobileOrder("sell")}>賣出</button>
    </div>}
  </div>;
}
