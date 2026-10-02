import { useEffect, useRef, useState } from "react";
import { apiBase } from "../lib/api-client";
import {
  acceptsFeedMessage,
  isSocketStale,
  reconnectDelayMs,
} from "./market-socket-policy";
import type {
  ConnectionStatus,
  FeedMessage,
  KBar,
  SymbolKey,
  Timeframe,
} from "./types";

type MarketSocketOptions = {
  symbol: SymbolKey;
  interval: Timeframe;
  loadHistory: (
    symbol: SymbolKey,
    interval: Timeframe,
    signal: AbortSignal,
  ) => Promise<KBar[]>;
  resetChart: () => void;
  updateChart: (bar: KBar) => void;
  clearStrategies: () => void;
  reloadStrategies: () => Promise<void>;
  reportError: (message: string) => void;
  setLatest: (bar: KBar | null) => void;
};

export function useMarketSocket({
  symbol,
  interval,
  loadHistory,
  resetChart,
  updateChart,
  clearStrategies,
  reloadStrategies,
  reportError,
  setLatest,
}: MarketSocketOptions) {
  const socketRef = useRef<WebSocket | null>(null);
  const generationRef = useRef(0);
  const attemptsRef = useRef(0);
  const lastMessageAtRef = useRef(0);
  const [status, setStatus] = useState<ConnectionStatus>("connecting");
  const [lastTick, setLastTick] = useState<string | null>(null);
  const [latency, setLatency] = useState<number | null>(null);

  useEffect(() => {
    const generation = ++generationRef.current;
    const abortController = new AbortController();
    let disposed = false;
    let activeSocket: WebSocket | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    const isCurrentGeneration = () => (
      !disposed && generationRef.current === generation
    );
    const isCurrentSocket = (socket: WebSocket) => (
      isCurrentGeneration()
      && activeSocket === socket
      && socketRef.current === socket
    );

    attemptsRef.current = 0;
    lastMessageAtRef.current = 0;
    let initialConnect = true;
    const connect = async () => {
      if (!isCurrentGeneration()) return;
      if (initialConnect) {
        initialConnect = false;
        setLatest(null);
        clearStrategies();
        resetChart();
      }
      setStatus(attemptsRef.current ? "reconnecting" : "connecting");
      try {
        const bars = await loadHistory(symbol, interval, abortController.signal);
        if (isCurrentGeneration() && bars.length) setLatest(bars.at(-1) ?? null);
      } catch (reason) {
        if (
          isCurrentGeneration()
          && !(reason instanceof DOMException && reason.name === "AbortError")
        ) {
          reportError(reason instanceof Error ? reason.message : "REST 載入失敗");
        }
      }
      if (!isCurrentGeneration()) return;
      const wsUrl = `${apiBase().replace(/^http/, "ws")}/ws/market/${symbol}?interval=${interval}`;
      const socket = new WebSocket(wsUrl);
      activeSocket = socket;
      socketRef.current = socket;
      socket.onopen = async () => {
        if (!isCurrentSocket(socket)) return;
        attemptsRef.current = 0;
        lastMessageAtRef.current = Date.now();
        reportError("");
        const bars = await loadHistory(
          symbol,
          interval,
          abortController.signal,
        ).catch(() => []);
        if (!isCurrentSocket(socket)) return;
        if (bars.length) setLatest(bars.at(-1) ?? null);
        await reloadStrategies();
      };
      socket.onmessage = event => {
        if (!isCurrentSocket(socket)) return;
        const message = JSON.parse(event.data) as FeedMessage;
        lastMessageAtRef.current = Date.now();
        setStatus(message.connection_status);
        if (!acceptsFeedMessage(message, interval)) return;
        if (message.type === "kbar") {
          updateChart(message);
          setLatest(message);
          setLastTick(message.exchange_time);
          setLatency(message.latency_ms);
          if (message.status === "closed") void reloadStrategies();
        } else {
          setLastTick(message.last_tick_time);
          setLatency(message.latency_ms);
        }
      };
      socket.onerror = () => {
        if (isCurrentSocket(socket)) reportError("WebSocket 連線發生錯誤");
      };
      socket.onclose = () => {
        if (!isCurrentSocket(socket)) return;
        socketRef.current = null;
        activeSocket = null;
        setStatus("reconnecting");
        const delay = reconnectDelayMs(attemptsRef.current, Math.random() * 300);
        attemptsRef.current += 1;
        reconnectTimer = setTimeout(() => {
          reconnectTimer = null;
          void connect();
        }, delay);
      };
    };

    void connect();
    const handleVisibilityChange = () => {
      if (document.visibilityState === "visible" && isCurrentGeneration()) {
        lastMessageAtRef.current = Date.now();
      }
    };
    document.addEventListener("visibilitychange", handleVisibilityChange);
    const watchdog = setInterval(() => {
      if (isCurrentGeneration() && isSocketStale({
        now: Date.now(),
        lastMessageAt: lastMessageAtRef.current,
        documentVisible: document.visibilityState === "visible",
        socketOpen: activeSocket?.readyState === WebSocket.OPEN,
      })) {
        setStatus("disconnected");
        activeSocket?.close();
      }
    }, 2_500);
    return () => {
      disposed = true;
      abortController.abort();
      document.removeEventListener("visibilitychange", handleVisibilityChange);
      clearInterval(watchdog);
      if (reconnectTimer) clearTimeout(reconnectTimer);
      if (socketRef.current === activeSocket) socketRef.current = null;
      activeSocket?.close();
      activeSocket = null;
    };
  }, [
    clearStrategies,
    interval,
    loadHistory,
    reloadStrategies,
    reportError,
    resetChart,
    setLatest,
    symbol,
    updateChart,
  ]);

  return { status, lastTick, latency };
}
