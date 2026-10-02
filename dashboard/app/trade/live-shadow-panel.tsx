"use client";

import { useEffect, useState } from "react";
import { apiRequest } from "../lib/api-client";
import { formatPrice, formatTaipeiDateTime } from "../lib/formatters";

type ShadowResult = {
  masked_account_id: string;
  strategy_id: string;
  runtime_id: string;
  action: string;
  direction: string;
  risk_status: string;
  execution_policy: string;
  best_bid: number | null;
  best_ask: number | null;
  result: "would_submit" | "rejected";
  reason: string;
  created_at: string;
};

type ShadowPayload = {
  mode: "live_shadow";
  ordering_enabled: false;
  results: ShadowResult[];
  kill_switch_preview: Array<{
    action: string; would_cancel: boolean; would_flatten: boolean; reason: string;
  }>;
};

export default function LiveShadowPanel() {
  const [payload, setPayload] = useState<ShadowPayload | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    const load = async () => {
      try {
        const value = await apiRequest<ShadowPayload>(
          "/api/live-shadow", { cache: "no-store" }, "Live Shadow 載入失敗",
        );
        if (active) { setPayload(value); setError(""); }
      } catch (reason) {
        if (active) setError(reason instanceof Error ? reason.message : "Live Shadow 載入失敗");
      }
    };
    void load();
    const timer = window.setInterval(() => void load(), 5_000);
    return () => { active = false; window.clearInterval(timer); };
  }, []);

  const latest = payload?.results[0] ?? null;
  return <section className="live-shadow-panel" aria-label="Live Shadow 執行可視性">
    <div className="panel-head">
      <div><span>PRE-LIVE OBSERVABILITY</span><h2>Live Shadow</h2></div>
      <strong className={`shadow-state ${latest?.result === "would_submit" ? "approved" : "blocked"}`}>
        {latest?.result === "would_submit" ? "WOULD SUBMIT" : latest ? "REJECTED" : "SHADOW ONLY"}
      </strong>
    </div>
    <p>真實行情與已 reconciled broker truth 的下單推演；沒有 Broker dispatch、Live outbox 或實盤操作按鈕。</p>
    {error && <div className="live-error">{error}</div>}
    {payload?.kill_switch_preview.map((item, index) => <div className="live-shadow-warning" key={`${item.action}-${index}`}>
      {item.would_flatten ? "WOULD CANCEL + FLATTEN" : item.would_cancel ? "WOULD CANCEL WORKING" : "HALT ENTRY"} · {item.reason}
    </div>)}
    {latest ? <div className="live-shadow-grid">
      <div><span>Execution Provider / Account</span><b>Configured / {latest.masked_account_id}</b></div>
      <div><span>Strategy / Runtime</span><b>{latest.strategy_id} / {latest.runtime_id.slice(0, 8)}</b></div>
      <div><span>Decision</span><b>{latest.action.toUpperCase()} · {latest.direction.toUpperCase()}</b></div>
      <div><span>Risk</span><b>{latest.risk_status.toUpperCase()}</b></div>
      <div><span>Execution Policy</span><b>{latest.execution_policy.replaceAll("_", " ").toUpperCase()}</b></div>
      <div><span>Executable Quote</span><b>{formatPrice(latest.best_bid)} / {formatPrice(latest.best_ask)}</b></div>
      <div><span>Would Submit</span><b>{latest.result === "would_submit" ? "YES" : "NO"}</b></div>
      <div><span>Reason / Time</span><b>{latest.reason} · {formatTaipeiDateTime(latest.created_at)}</b></div>
    </div> : <div className="live-shadow-empty">尚無 Shadow evaluation。Live Shadow 必須由 server-side 設定明確啟用及指定 execution target。</div>}
  </section>;
}
