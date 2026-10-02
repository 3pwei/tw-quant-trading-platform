"use client";

import { useEffect, useState } from "react";
import { apiRequest } from "../lib/api-client";

type Runtime = { runtime_id: string; status: string; strategy_id: string; strategy_version?: number; interval: string; quantity: number; masked_account_id?: string; arm_active?: boolean; preflight?: Record<string, boolean> };
type Status = { enabled: boolean; real_money: true; runtimes: Runtime[] };

export default function LiveAutoPanel() {
  const [status, setStatus] = useState<Status | null>(null);
  const [confirmation, setConfirmation] = useState("");
  const [message, setMessage] = useState("");
  useEffect(() => { void apiRequest<Status>("/api/live-auto", { cache: "no-store" }, "Live Auto unavailable").then(setStatus).catch(() => setStatus(null)); }, []);
  if (!status) return null;
  return <section className="live-canary-panel" aria-label="Strategy Auto Live">
    <div className="panel-head"><div><span>REAL MONEY · STRATEGY AUTO</span><h2>Live Auto</h2></div><strong>{status.enabled ? "DISARMED" : "DISABLED"}</strong></div>
    <p>Explicit, time-limited ARM only. Restart, reconnect, Recovery Lock, stale market, Guardian degradation, UNKNOWN order, or Kill Switch blocks new exposure.</p>
    {!status.enabled && <p className="live-canary-warning">LIVE_AUTO_ENABLED=false · No strategy decision can reach real execution.</p>}
    {status.runtimes.map(runtime => <article key={runtime.runtime_id} className="live-auto-runtime">
      <b>{runtime.strategy_id} v{runtime.strategy_version ?? 1} · {runtime.interval} · Qty {runtime.quantity}</b>
      <span>Execution Provider · {runtime.masked_account_id ?? "—"} · {runtime.arm_active ? "ARMED" : runtime.status.toUpperCase()}</span>
      <span>Execution policy and Live Risk are immutable in the runtime snapshot · Guardian {runtime.preflight?.guardian_healthy ? "HEALTHY" : "NOT READY"}</span>
      <small>Guardian coordinates every Strategy Exit, Stop Loss, Take Profit and Emergency Flatten.</small>
      <label><span>Type exactly: ARM LIVE AUTO - REAL MONEY</span><input aria-label="Live Auto ARM confirmation" value={confirmation} onChange={event => setConfirmation(event.target.value)} /></label>
      <div className="canary-arm-row">
        <button disabled={!status.enabled || runtime.arm_active || confirmation !== "ARM LIVE AUTO - REAL MONEY"} onClick={() => void apiRequest(`/api/trading-runtimes/${runtime.runtime_id}/live-auto/arm`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ confirmation }) }, "ARM rejected").then(() => setMessage("ARM active; operator must remain present.")).catch(error => setMessage(error instanceof Error ? error.message : "ARM rejected"))}>ARM LIVE AUTO</button>
        <button disabled={!runtime.arm_active} onClick={() => void apiRequest(`/api/trading-runtimes/${runtime.runtime_id}/live-auto/disarm`, { method: "POST" }, "Disarm rejected").then(() => setMessage("DISARMED; Guardian protection remains active.")).catch(error => setMessage(error instanceof Error ? error.message : "Disarm rejected"))}>DISARM</button>
      </div>
    </article>)}
    {message && <div className="live-canary-message">{message}</div>}
  </section>;
}
