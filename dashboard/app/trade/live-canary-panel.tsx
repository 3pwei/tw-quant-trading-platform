"use client";

import { useCallback, useEffect, useState } from "react";
import { apiRequest } from "../lib/api-client";

type LiveOrder = {
  platform_order_id: string; broker_order_id: string | null; side: "buy" | "sell";
  requested_quantity: number; filled_quantity: number; order_type: string;
  time_in_force: string; limit_price: number | null; status: string;
  status_reason: string | null; operator_warning?: string;
};
type CanaryStatus = {
  enabled: boolean; mode: "live_canary"; real_money: true;
  masked_account_id: string; arm: "active" | "off"; arm_expires_at: string | null;
  recovery_status: string; broker_connected: boolean; ca_ready: boolean;
  position: "flat" | "long" | "short" | "unknown"; position_quantity: number | null;
  max_quantity: 1; allowed_symbol: string; allowed_contract: string; orders: LiveOrder[];
  position_guardian?: { enabled: boolean; platform_managed?: boolean; managed_positions?: number; protected_quantity?: number; active_exits?: number; locked_positions?: number };
};

export default function LiveCanaryPanel() {
  const [status, setStatus] = useState<CanaryStatus | null>(null);
  const [armText, setArmText] = useState("");
  const [orderText, setOrderText] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const load = useCallback(async () => {
    try {
      const value = await apiRequest<CanaryStatus>("/api/live/canary", { cache: "no-store" }, "Live Canary unavailable");
      setStatus(value); setMessage("");
    } catch { setStatus(null); }
  }, []);
  useEffect(() => {
    const initial = window.setTimeout(() => void load(), 0);
    const timer = window.setInterval(() => void load(), 4_000);
    return () => { window.clearTimeout(initial); window.clearInterval(timer); };
  }, [load]);
  if (!status?.enabled) return null;

  const action = async (work: () => Promise<unknown>) => {
    setBusy(true); setMessage("");
    try { await work(); setMessage("Request durably recorded; verify broker truth."); await load(); }
    catch (reason) { setMessage(reason instanceof Error ? reason.message : "Request rejected"); }
    finally { setBusy(false); }
  };
  const order = (side: "buy" | "sell") => action(() => apiRequest(
    "/api/live/orders",
    { method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": crypto.randomUUID() }, body: JSON.stringify({ side, quantity: 1, confirmation: orderText }) },
    "Real order rejected",
  ));
  return <section className="live-canary-panel" aria-label="Manual Live Order Canary">
    <div className="panel-head"><div><span>REAL MONEY · MANUAL ONLY</span><h2>Live Canary</h2></div><strong>{status.arm === "active" ? "ARM ACTIVE" : "ARM OFF"}</strong></div>
    <p>Execution Provider · {status.masked_account_id} · {status.allowed_contract} · Max Qty 1 · Recovery {status.recovery_status.toUpperCase()} · Position {status.position.toUpperCase()} {status.position_quantity ?? "—"}</p>
    <p className="live-canary-warning">POSITION PROTECTION IS PLATFORM-MANAGED — it depends on the execution worker, market data, network, and broker connectivity. It is not a broker-native stop/OCO.</p>
    <p>Guardian {status.position_guardian?.enabled ? "ACTIVE" : "DISABLED"} · Managed {status.position_guardian?.managed_positions ?? 0} · Protected Qty {status.position_guardian?.protected_quantity ?? 0} · Active Exits {status.position_guardian?.active_exits ?? 0}</p>
    <div className="canary-arm-row">
      <input aria-label="Live Canary ARM confirmation" value={armText} onChange={event => setArmText(event.target.value)} placeholder="I_UNDERSTAND_MANUAL_LIVE_CANARY" />
      <button disabled={busy || status.arm === "active"} onClick={() => void action(() => apiRequest("/api/live/canary/arm", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ confirmation: armText }) }, "ARM rejected"))}>ARM LIVE CANARY</button>
      <button disabled={busy || status.arm !== "active"} onClick={() => void action(() => apiRequest("/api/live/canary/arm", { method: "DELETE" }, "Disarm failed"))}>DISARM</button>
    </div>
    <label className="canary-order-confirm"><span>Type the exact order confirmation, e.g. BUY 1 {status.allowed_contract} REAL ORDER</span><input value={orderText} onChange={event => setOrderText(event.target.value)} /></label>
    <div className="canary-order-actions">
      <button className="buy" disabled={busy || status.arm !== "active"} onClick={() => void order("buy")}>BUY 1 · REAL ORDER</button>
      <button className="sell" disabled={busy || status.arm !== "active"} onClick={() => void order("sell")}>SELL 1 · REAL ORDER</button>
      <button disabled={busy || status.arm !== "active"} onClick={() => void action(() => apiRequest("/api/live/position/close", { method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": crypto.randomUUID() }, body: JSON.stringify({ confirmation: "REAL ORDER" }) }, "Close rejected"))}>CLOSE POSITION · REDUCE ONLY</button>
    </div>
    <div className="canary-kill-actions">
      <button disabled={busy || status.arm !== "active"} onClick={() => void action(() => apiRequest("/api/live/canary/kill-switch", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "halt_entry", reason: "manual operator halt", confirmation: "REAL ORDER" }) }, "HALT rejected"))}>HALT ENTRY</button>
      <button disabled={busy || status.arm !== "active"} onClick={() => void action(() => apiRequest("/api/live/canary/kill-switch", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "cancel_working", reason: "manual operator cancel", confirmation: "REAL ORDER" }) }, "Cancel working rejected"))}>CANCEL WORKING</button>
      <button className="kill" disabled={busy || !status.position_guardian?.enabled} onClick={() => void action(() => apiRequest("/api/live/canary/kill-switch", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "flatten", reason: "manual emergency flatten", confirmation: "REAL ORDER" }) }, "Emergency flatten rejected"))}>EMERGENCY FLATTEN · REDUCE ONLY</button>
    </div>
    {message && <div className="live-canary-message">{message}</div>}
    <div className="live-canary-orders">{status.orders.map(item => <article key={item.platform_order_id} className={item.status === "unknown" ? "unknown" : ""}>
      <b>{item.side.toUpperCase()} {item.requested_quantity} · {item.status.toUpperCase()}</b><span>{item.filled_quantity}/{item.requested_quantity} · {item.order_type.toUpperCase()} {item.time_in_force.toUpperCase()} · {item.limit_price ?? "—"}</span><small>{item.platform_order_id} · Provider Order {item.broker_order_id ?? "pending"}</small>
      {item.operator_warning && <strong>{item.operator_warning}</strong>}
      {item.status === "unknown" && <strong>UNKNOWN · DO NOT RETRY · RECONCILIATION REQUIRED</strong>}
      {!new Set(["filled", "cancelled", "rejected", "expired", "unknown"]).has(item.status) && <button disabled={busy || status.arm !== "active"} onClick={() => void action(() => apiRequest(`/api/live/orders/${item.platform_order_id}/cancel`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ confirmation: "REAL ORDER" }) }, "Cancel rejected"))}>CANCEL ORDER</button>}
    </article>)}</div>
  </section>;
}
