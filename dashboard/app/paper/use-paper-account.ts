import { useCallback, useEffect, useState } from "react";
import { apiRequest } from "../lib/api-client";
import { loadPaperAccountData } from "./paper-account-loader";
import type { Account, CurrentUser, PaperFill, PaperOrder, PaperOverlaySnapshot, PaperPosition } from "./types";

export function usePaperAccount(
  onOverlayChange: (snapshot: PaperOverlaySnapshot) => void,
) {
  const [user, setUser] = useState<CurrentUser | null>(null);
  const [account, setAccount] = useState<Account | null>(null);
  const [positions, setPositions] = useState<PaperPosition[]>([]);
  const [orders, setOrders] = useState<PaperOrder[]>([]);
  const [fills, setFills] = useState<PaperFill[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const load = useCallback(async (silent = false) => {
    try {
      const data = await loadPaperAccountData(apiRequest);
      setUser(data.user);
      setAccount(data.account);
      setPositions(data.positions);
      setOrders(data.orders);
      setFills(data.fills);
      onOverlayChange({
        positions: data.positions,
        orders: data.orders,
        fills: data.fills,
      });
      if (!silent) setError("");
    } catch (reason) {
      if (!silent) {
        setError(reason instanceof Error ? reason.message : "無法載入模擬帳戶");
      }
    } finally {
      setLoading(false);
    }
  }, [onOverlayChange]);

  useEffect(() => {
    const initial = window.setTimeout(() => void load(), 0);
    const timer = window.setInterval(() => void load(true), 5_000);
    return () => {
      window.clearTimeout(initial);
      window.clearInterval(timer);
    };
  }, [load]);

  return {
    user,
    account,
    positions,
    orders,
    fills,
    error,
    setError,
    loading,
    load,
  };
}
