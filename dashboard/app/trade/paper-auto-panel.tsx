"use client";

import { useState } from "react";
import { formatPrice, formatTaipeiDateTime } from "../lib/formatters";
import type { StrategyOption, Timeframe } from "../live/types";
import type {
  Account,
  CurrentUser,
  MarketHealth,
  PaperFill,
  PaperOrder,
  PaperPosition,
} from "../paper/types";
import { automationState, canUsePaperAuto, runtimeActions } from "./paper-auto-policy";
import { usePaperAuto } from "./use-paper-auto";

type PaperAutoPanelProps = {
  user: CurrentUser | null;
  account: Account | null;
  marketHealth: MarketHealth | null;
  strategyOptions: StrategyOption[];
  selectedStrategies: string[];
  symbol: string;
  interval: Timeframe;
  positions: PaperPosition[];
  orders: PaperOrder[];
  fills: PaperFill[];
  busy: boolean;
  onClosePosition: (position: PaperPosition) => Promise<void>;
  onKillSwitch: () => Promise<void>;
  onStrategySelected: (strategy: string) => void;
};

const intervalLabels: Record<Timeframe, string> = {
  "1m": "1 分 K", "5m": "5 分 K", "10m": "10 分 K", "15m": "15 分 K",
  "30m": "30 分 K", "1h": "1 小時 K", "1d": "日 K", "1w": "週 K",
};

function displayValue(value: unknown): string {
  if (typeof value === "number") return Number.isInteger(value) ? String(value) : String(Number(value.toFixed(6)));
  if (typeof value === "boolean") return value ? "是" : "否";
  return value == null ? "—" : String(value);
}

export default function PaperAutoPanel({
  user,
  account,
  marketHealth,
  strategyOptions,
  selectedStrategies,
  symbol,
  interval,
  positions,
  orders,
  fills,
  busy,
  onClosePosition,
  onKillSwitch,
  onStrategySelected,
}: PaperAutoPanelProps) {
  const enabled = Boolean(user?.permissions.includes("strategy.read.own"));
  const auto = usePaperAuto(enabled);
  const [draftStrategy, setDraftStrategy] = useState("");
  const [draftInterval, setDraftInterval] = useState<Timeframe>(interval);
  const [draftQuantity, setDraftQuantity] = useState(1);
  const [actionBusy, setActionBusy] = useState("");
  const [notice, setNotice] = useState("");

  const effectiveDraftStrategy = draftStrategy || selectedStrategies[0] || strategyOptions[0]?.key || "";

  const runtime = auto.selectedRuntime;
  const state = automationState(runtime, account, marketHealth);
  const actions = runtimeActions(runtime, user);
  const currentPosition = positions.find(item => item.runtime_id === runtime?.runtime_id) ?? null;
  const runtimeOrders = orders.filter(item => item.runtime_id === runtime?.runtime_id);
  const runtimeFills = fills.filter(item => item.runtime_id === runtime?.runtime_id);
  const latestOrder = runtimeOrders[0] ?? null;
  const latestFill = runtimeFills[0] ?? null;
  const latestDecision = auto.decisions[0] ?? null;
  const decisionFill = runtimeFills.find(item => item.decision_id === latestDecision?.decision_id) ?? null;
  const strategyName = strategyOptions.find(item => item.key === runtime?.strategy_id)?.name
    ?? runtime?.strategy_id
    ?? "尚未建立";
  const parameters = runtime?.strategy_snapshot.parameters ?? {};
  const paperAutoAllowed = canUsePaperAuto(user);
  const riskLimits = account?.risk_limits;
  const executionText = !latestDecision
    ? "尚無決策"
    : latestDecision.execution_status === "filled"
      ? `FILLED · ${formatTaipeiDateTime(decisionFill?.meta.occurred_at)} @ ${formatPrice(latestDecision.actual_fill_price ?? decisionFill?.price)}`
      : `${latestDecision.execution_status.toUpperCase()}${latestDecision.execution_reason ? ` · ${latestDecision.execution_reason}` : ""}`;

  async function createSnapshot() {
    if (!effectiveDraftStrategy) return;
    setActionBusy("create");
    auto.setError("");
    setNotice("");
    try {
      onStrategySelected(effectiveDraftStrategy);
      const created = await auto.create({
        strategy_id: effectiveDraftStrategy,
        symbol,
        interval: draftInterval,
        quantity: draftQuantity,
      });
      setNotice(`已建立 ${created.strategy_id} 不可變快照；確認後才能 ARM。`);
    } catch (reason) {
      auto.setError(reason instanceof Error ? reason.message : "建立 Runtime 失敗");
    } finally {
      setActionBusy("");
    }
  }

  async function runAction(action: "arm" | "pause" | "stop") {
    if (!runtime) return;
    const confirmation = action === "arm"
      ? `確認 ARM PAPER AUTO？\n\n策略：${strategyName}\n週期：${runtime.interval}\n數量：${runtime.quantity} 口\n\n系統只會送往模擬券商，不會送出真實委託。`
      : action === "stop"
        ? `停止 Runtime？${currentPosition ? "\n\n現有部位不會被強制平倉，伺服器仍會依停損／停利管理至平倉。" : ""}`
        : "暫停新的自動進場？既有部位的策略出場、停損與停利仍會繼續管理。";
    if (!window.confirm(confirmation)) return;
    setActionBusy(action);
    auto.setError("");
    setNotice("");
    try {
      await auto.control(action);
      setNotice(action === "arm" ? "PAPER AUTO 已 ARM" : action === "pause" ? "Runtime 已暫停新進場" : "Runtime 已停止");
    } catch (reason) {
      auto.setError(reason instanceof Error ? reason.message : `Runtime ${action} 失敗`);
    } finally {
      setActionBusy("");
    }
  }

  return <section className="paper-auto panel" aria-label="Paper Auto 自動交易控制">
    <div className="panel-head">
      <div><span>AUTOMATION</span><h2>Paper Auto</h2></div>
      <strong className={`automation-state ${state.tone}`}>{state.label}</strong>
    </div>
    <p className="paper-auto-boundary">Closed K 決策 → next-bar-open 模擬成交。Automated Paper Trading 不等於 Live Trading。</p>

    {!paperAutoAllowed && <div className="paper-auto-denied">此帳號只能 Observe。ARM 需要 Paper trading mode 與 <code>orders.paper</code> 權限。</div>}
    {(auto.error || notice) && <div className={`paper-message ${auto.error ? "error" : "success"}`}>{auto.error || notice}</div>}

    <div className="paper-auto-builder">
      <label><span>策略</span><select value={effectiveDraftStrategy} onChange={event => { setDraftStrategy(event.target.value); onStrategySelected(event.target.value); }}>{strategyOptions.map(option => <option key={option.key} value={option.key}>{option.name}</option>)}</select></label>
      <label><span>Interval</span><select value={draftInterval} onChange={event => setDraftInterval(event.target.value as Timeframe)}>{Object.entries(intervalLabels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
      <label><span>Quantity</span><select value={draftQuantity} onChange={event => setDraftQuantity(Number(event.target.value))}><option value={1}>1 口</option><option value={2}>2 口</option></select></label>
      <button type="button" disabled={!paperAutoAllowed || !effectiveDraftStrategy || Boolean(actionBusy)} onClick={() => void createSnapshot()}>{actionBusy === "create" ? "建立中…" : "建立並檢查快照"}</button>
    </div>

    {auto.runtimes.length > 0 && <label className="paper-auto-runtime-select"><span>Runtime</span><select value={auto.selectedRuntimeId ?? ""} onChange={event => auto.setSelectedRuntimeId(event.target.value)}>{auto.runtimes.map(item => <option key={item.runtime_id} value={item.runtime_id}>{item.strategy_id} · {item.interval} · {item.status}</option>)}</select></label>}

    {runtime && <>
      <div className="paper-auto-details">
        <div><span>Strategy / Version</span><b>{strategyName} · {runtime.strategy_version ? `v${runtime.strategy_version}` : "Atomic snapshot"}</b></div>
        <div><span>Mode / Status</span><b>PAPER AUTO · {runtime.status.toUpperCase()}</b></div>
        <div><span>Symbol / Interval</span><b>{runtime.symbol} · {intervalLabels[runtime.interval]}</b></div>
        <div><span>Quantity</span><b>{runtime.quantity} 口</b></div>
        <div><span>Last evaluated bar</span><b>{formatTaipeiDateTime(runtime.last_evaluated_bar)}</b></div>
        <div><span>Last decision</span><b>{formatTaipeiDateTime(latestDecision?.trigger_time ?? runtime.last_evaluated_bar)}</b></div>
        <div><span>Last order</span><b>{latestOrder ? `${latestOrder.status.toUpperCase()} · ${formatTaipeiDateTime(latestOrder.submitted_at)}` : "—"}</b></div>
        <div><span>Last fill</span><b>{latestFill ? `${formatTaipeiDateTime(latestFill.meta.occurred_at)} @ ${formatPrice(latestFill.price)}` : "—"}</b></div>
        <div><span>Current position</span><b>{currentPosition ? `${currentPosition.quantity > 0 ? "LONG" : "SHORT"} ${Math.abs(currentPosition.quantity)} 口 @ ${formatPrice(currentPosition.average_price)}` : "FLAT"}</b></div>
        <div><span>Stop Loss / Take Profit</span><b>{currentPosition ? `${formatPrice(currentPosition.stop_loss_price)} / ${formatPrice(currentPosition.take_profit_price)}` : "—"}</b></div>
        <div><span>Market health</span><b>{marketHealth?.service_status?.toUpperCase() ?? "UNKNOWN"}</b></div>
        <div><span>Risk health</span><b>{account?.recovery_status === "healthy" && !account.kill_switch_active ? "HEALTHY" : account?.kill_switch_active ? "KILL SWITCH" : "RECOVERY LOCK"}</b></div>
      </div>

      <details className="paper-auto-parameters">
        <summary>Strategy snapshot parameters · {Object.keys(parameters).length}</summary>
        <dl>{Object.entries(parameters).map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{displayValue(value)}</dd></div>)}</dl>
      </details>

      <section className="paper-auto-decision">
        <span>LATEST DECISION</span>
        {latestDecision ? <>
          <h3>{formatTaipeiDateTime(latestDecision.trigger_time)} · {strategyName}</h3>
          <p>{latestDecision.reason}</p>
          <dl><div><dt>Decision</dt><dd>{latestDecision.action.toUpperCase()} · {latestDecision.direction.toUpperCase()}</dd></div><div><dt>Execution</dt><dd>{executionText}</dd></div></dl>
        </> : <p>等待下一個 closed-bar strategy decision。</p>}
      </section>

      <div className="paper-auto-controls">
        {actions.includes("arm") && <button type="button" className="arm" disabled={!paperAutoAllowed || Boolean(actionBusy)} onClick={() => void runAction("arm")}>{runtime.status === "paused" ? "RESUME PAPER AUTO" : "ARM PAPER AUTO"}</button>}
        {actions.includes("pause") && <button type="button" className="pause" disabled={Boolean(actionBusy)} onClick={() => void runAction("pause")}>PAUSE</button>}
        {actions.includes("stop") && <button type="button" className="stop" disabled={Boolean(actionBusy)} onClick={() => void runAction("stop")}>{currentPosition ? "STOP · KEEP MANAGED UNTIL FLAT" : "STOP RUNTIME"}</button>}
        <button type="button" className="kill" disabled={!paperAutoAllowed || busy || Boolean(actionBusy) || Boolean(account?.kill_switch_active)} onClick={() => void onKillSwitch()}>KILL SWITCH</button>
        {currentPosition && <button type="button" className="emergency" disabled={busy} onClick={() => void onClosePosition(currentPosition)}>手動緊急平倉</button>}
      </div>
      {runtime.status === "stopped" && currentPosition && <p className="paper-auto-draining">Runtime 已停止；不再建立新決策。現有部位仍由 server-side Stop Loss／Take Profit 管理至平倉，也可手動緊急平倉。</p>}
    </>}

    <div className="paper-auto-risk-limits">
      <span>ACCOUNT RISK LIMITS</span>
      <dl>
        <div><dt>最大持倉</dt><dd>{riskLimits ? `${riskLimits.max_position_contracts} 口` : "—"}</dd></div>
        <div><dt>單筆最大風險</dt><dd>{riskLimits ? `NT$ ${riskLimits.max_risk_per_trade.toLocaleString()}` : "—"}</dd></div>
        <div><dt>每日最大損失</dt><dd>{riskLimits ? `NT$ ${riskLimits.max_daily_loss.toLocaleString()}` : "—"}</dd></div>
        <div><dt>每日交易上限</dt><dd>{riskLimits ? `${riskLimits.max_trades_per_day} 筆` : "—"}</dd></div>
      </dl>
    </div>
  </section>;
}
