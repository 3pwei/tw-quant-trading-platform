import type { Account, CurrentUser, MarketHealth, PaperFill } from "../paper/types";
import type { RuntimeAction, TradingRuntime } from "./paper-auto-types";

export type AutomationState = {
  code: "observe" | "paused" | "armed" | "recovery_locked" | "market_blocked" | "kill_switch" | "stopped";
  label: string;
  tone: "neutral" | "good" | "warning" | "danger";
  permitsEntry: boolean;
};

export function canUsePaperAuto(user: CurrentUser | null): boolean {
  return Boolean(
    user
    && user.trading_mode === "paper"
    && user.permissions.includes("orders.paper"),
  );
}

export function automationState(
  runtime: TradingRuntime | null,
  account: Account | null,
  market: MarketHealth | null,
): AutomationState {
  if (!runtime) return { code: "observe", label: "OBSERVE", tone: "neutral", permitsEntry: false };
  if (runtime.status === "recovery_locked" || runtime.recovery_issue) {
    return { code: "recovery_locked", label: "RECOVERY LOCK", tone: "danger", permitsEntry: false };
  }
  if (runtime.status === "stopped") {
    return { code: "stopped", label: "STOPPED", tone: "danger", permitsEntry: false };
  }
  if (account?.kill_switch_active) {
    return { code: "kill_switch", label: "KILL SWITCH", tone: "danger", permitsEntry: false };
  }
  if (market?.service_status !== "healthy" || market.trading_block_reason) {
    return {
      code: "market_blocked",
      label: market?.trading_block_reason === "provider_disconnected" ? "PROVIDER DISCONNECTED" : "MARKET STALE",
      tone: "danger",
      permitsEntry: false,
    };
  }
  if (runtime.status === "armed") {
    return { code: "armed", label: "PAPER AUTO · ARMED", tone: "good", permitsEntry: true };
  }
  return { code: "paused", label: "PAUSED", tone: "warning", permitsEntry: false };
}

export function runtimeActions(
  runtime: TradingRuntime | null,
  user: CurrentUser | null,
): RuntimeAction[] {
  if (!runtime || runtime.status === "stopped") return [];
  if (runtime.status === "armed") return ["pause", "stop"];
  return canUsePaperAuto(user) && !runtime.recovery_issue
    ? ["arm", "stop"]
    : ["stop"];
}

export function paperFillAppearance(fill: Pick<PaperFill, "order_source" | "purpose" | "side">) {
  const auto = fill.order_source === "strategy_auto";
  return {
    prefix: auto ? "AUTO" : "MANUAL",
    color: auto
      ? fill.purpose === "entry" ? "#38bdf8" : "#f59e0b"
      : fill.side === "buy" ? "#42d6a4" : "#ff6b72",
    shape: auto && fill.purpose !== "entry"
      ? "square" as const
      : fill.side === "buy" ? "arrowUp" as const : "arrowDown" as const,
  };
}
