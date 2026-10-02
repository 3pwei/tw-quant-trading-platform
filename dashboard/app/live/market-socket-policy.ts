import type { FeedMessage, Timeframe } from "./types";

export const SOCKET_STALE_AFTER_MS = 45_000;
export const MAX_RECONNECT_DELAY_MS = 30_000;

export function reconnectDelayMs(attempt: number, jitterMs = 0): number {
  return Math.min(MAX_RECONNECT_DELAY_MS, 1_000 * 2 ** Math.max(0, attempt))
    + Math.max(0, jitterMs);
}

export function acceptsFeedMessage(
  message: FeedMessage,
  interval: Timeframe,
): boolean {
  return message.type !== "kbar" || message.interval === interval;
}

type SocketStaleness = {
  now: number;
  lastMessageAt: number;
  documentVisible: boolean;
  socketOpen: boolean;
};

export function isSocketStale({
  now,
  lastMessageAt,
  documentVisible,
  socketOpen,
}: SocketStaleness): boolean {
  return documentVisible
    && socketOpen
    && lastMessageAt > 0
    && now - lastMessageAt > SOCKET_STALE_AFTER_MS;
}
