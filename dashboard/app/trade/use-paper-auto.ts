import { useCallback, useEffect, useState } from "react";
import { apiRequest, jsonRequest } from "../lib/api-client";
import type { Timeframe } from "../live/types";
import type { TradingDecision, TradingRuntime } from "./paper-auto-types";

type RuntimeCreate = {
  strategy_id: string;
  symbol: string;
  interval: Timeframe;
  quantity: number;
};

export function usePaperAuto(enabled: boolean) {
  const [runtimes, setRuntimes] = useState<TradingRuntime[]>([]);
  const [selectedRuntimeId, setSelectedRuntimeId] = useState<string | null>(null);
  const [decisions, setDecisions] = useState<TradingDecision[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async (silent = false, preferredRuntimeId?: string) => {
    if (!enabled) {
      setRuntimes([]);
      setDecisions([]);
      return;
    }
    if (!silent) setLoading(true);
    try {
      const body = await apiRequest<{ runtimes: TradingRuntime[] }>(
        "/api/trading-runtimes",
        { cache: "no-store" },
        "自動交易設定載入失敗",
      );
      const paperRuntimes = body.runtimes.filter(item => item.mode === "paper_auto");
      setRuntimes(paperRuntimes);
      const selected = paperRuntimes.find(item => item.runtime_id === (preferredRuntimeId ?? selectedRuntimeId))
        ?? paperRuntimes.find(item => item.status !== "stopped")
        ?? paperRuntimes[0]
        ?? null;
      setSelectedRuntimeId(selected?.runtime_id ?? null);
      if (selected) {
        const decisionBody = await apiRequest<{ decisions: TradingDecision[] }>(
          `/api/trading-runtimes/${selected.runtime_id}/decisions?limit=20`,
          { cache: "no-store" },
          "策略決策載入失敗",
        );
        setDecisions(decisionBody.decisions);
      } else {
        setDecisions([]);
      }
      if (!silent) setError("");
    } catch (reason) {
      if (!silent) setError(reason instanceof Error ? reason.message : "自動交易設定載入失敗");
    } finally {
      if (!silent) setLoading(false);
    }
  }, [enabled, selectedRuntimeId]);

  useEffect(() => {
    const initial = window.setTimeout(() => void load(), 0);
    if (!enabled) return () => window.clearTimeout(initial);
    const timer = window.setInterval(() => void load(true), 5_000);
    return () => {
      window.clearTimeout(initial);
      window.clearInterval(timer);
    };
  }, [enabled, load]);

  const create = useCallback(async (draft: RuntimeCreate) => {
    const runtime = await apiRequest<TradingRuntime>(
      "/api/trading-runtimes",
      jsonRequest("POST", { ...draft, strategy_kind: "atomic", mode: "paper_auto" }),
      "Paper Auto Runtime 建立失敗",
    );
    setSelectedRuntimeId(runtime.runtime_id);
    await load(true, runtime.runtime_id);
    return runtime;
  }, [load]);

  const control = useCallback(async (action: "arm" | "pause" | "stop") => {
    if (!selectedRuntimeId) throw new Error("尚未選擇 Runtime");
    await apiRequest<TradingRuntime>(
      `/api/trading-runtimes/${selectedRuntimeId}/${action}`,
      { method: "POST" },
      `Runtime ${action} 失敗`,
    );
    await load(true);
  }, [load, selectedRuntimeId]);

  const selectedRuntime = runtimes.find(item => item.runtime_id === selectedRuntimeId) ?? null;
  return {
    runtimes,
    selectedRuntime,
    selectedRuntimeId,
    setSelectedRuntimeId,
    decisions,
    loading,
    error,
    setError,
    load,
    create,
    control,
  };
}
