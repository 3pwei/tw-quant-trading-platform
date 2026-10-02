import type { TradeSelection } from "./types";

export function createInitialTradeSelection(): TradeSelection {
  return {
    symbol: "TMF",
    interval: "1m",
    strategies: [],
  };
}
