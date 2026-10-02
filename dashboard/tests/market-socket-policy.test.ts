import assert from "node:assert/strict";
import test from "node:test";
import {
  acceptsFeedMessage,
  isSocketStale,
  reconnectDelayMs,
  SOCKET_STALE_AFTER_MS,
} from "../app/live/market-socket-policy.ts";
import type { FeedMessage } from "../app/live/types.ts";

test("reconnect delay grows exponentially and caps before jitter", () => {
  assert.equal(reconnectDelayMs(0), 1_000);
  assert.equal(reconnectDelayMs(4), 16_000);
  assert.equal(reconnectDelayMs(8, 125), 30_125);
});

test("a K bar for another interval is rejected before updating the chart", () => {
  const wrongInterval = {
    type: "kbar",
    interval: "5m",
  } as FeedMessage;
  const status = { type: "status" } as FeedMessage;
  assert.equal(acceptsFeedMessage(wrongInterval, "1m"), false);
  assert.equal(acceptsFeedMessage(status, "1m"), true);
});

test("watchdog closes only a visible open socket with an expired heartbeat", () => {
  const stale = {
    now: 100_000,
    lastMessageAt: 100_000 - SOCKET_STALE_AFTER_MS - 1,
    documentVisible: true,
    socketOpen: true,
  };
  assert.equal(isSocketStale(stale), true);
  assert.equal(isSocketStale({ ...stale, documentVisible: false }), false);
  assert.equal(isSocketStale({ ...stale, socketOpen: false }), false);
  assert.equal(isSocketStale({ ...stale, lastMessageAt: 0 }), false);
});
