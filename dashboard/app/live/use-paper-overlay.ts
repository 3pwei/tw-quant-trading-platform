import { useEffect, useRef, useState, type RefObject } from "react";
import {
  LineStyle,
  type IPriceLine,
  type ISeriesApi,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import { formatPrice } from "../lib/formatters";
import type { PaperOverlaySnapshot } from "../paper/types";
import { paperFillAppearance } from "../trade/paper-auto-policy";

function atOrBefore(value: string, times: UTCTimestamp[]): UTCTimestamp | null {
  const target = Math.floor(Date.parse(value) / 1000) as UTCTimestamp;
  for (let index = times.length - 1; index >= 0; index -= 1) {
    if (times[index] <= target) return times[index];
  }
  return null;
}

type PaperOverlayOptions = {
  snapshot: PaperOverlaySnapshot;
  symbol: string;
  contract?: string;
  barTimesRef: RefObject<UTCTimestamp[]>;
  candleRef: RefObject<ISeriesApi<"Candlestick"> | null>;
  barRevision: string | number;
};

export function usePaperOverlay({
  snapshot,
  symbol,
  contract,
  barTimesRef,
  candleRef,
  barRevision,
}: PaperOverlayOptions): SeriesMarker<Time>[] {
  const priceLinesRef = useRef<IPriceLine[]>([]);
  const [fillMarkers, setFillMarkers] = useState<SeriesMarker<Time>[]>([]);

  useEffect(() => {
    setFillMarkers(snapshot.fills.flatMap(fill => {
      if (fill.symbol !== symbol || (contract && fill.contract !== contract)) return [];
      const time = atOrBefore(fill.meta.occurred_at, barTimesRef.current);
      if (time == null) return [];
      const appearance = paperFillAppearance(fill);
      return [{
        time,
        position: fill.side === "buy" ? "belowBar" as const : "aboveBar" as const,
        color: appearance.color,
        shape: appearance.shape,
        text: `${appearance.prefix} ${fill.purpose === "entry" ? "ENTRY" : "EXIT"} ${fill.quantity}口 @ ${formatPrice(fill.price)}`,
      }];
    }));
  }, [barRevision, barTimesRef, contract, snapshot.fills, symbol]);

  useEffect(() => {
    const series = candleRef.current;
    if (!series) return;
    priceLinesRef.current.forEach(line => series.removePriceLine(line));
    priceLinesRef.current = [];
    const positions = snapshot.positions.filter(position => (
      position.symbol === symbol
      && (!contract || position.contract === contract)
    ));
    for (const position of positions) {
      priceLinesRef.current.push(series.createPriceLine({
        price: position.average_price,
        color: position.quantity > 0 ? "#42d6a4" : "#ff6b72",
        lineWidth: 2,
        lineStyle: LineStyle.Dashed,
        axisLabelVisible: true,
        title: position.order_source === "strategy_auto"
          ? `AUTO · ${position.strategy_id} v${position.strategy_version} 均價`
          : `MANUAL ${position.quantity > 0 ? "多" : "空"}均價 ${Math.abs(position.quantity)}口`,
      }));
      const entryOrder = snapshot.orders.find(order => (
        order.status === "filled"
        && !order.reduce_only
        && order.strategy_id === position.strategy_id
        && order.strategy_version === position.strategy_version
        && order.contract === position.contract
        && order.stop_loss_price != null
      ));
      const stopLoss = position.stop_loss_price ?? entryOrder?.stop_loss_price;
      if (stopLoss != null) {
        priceLinesRef.current.push(series.createPriceLine({
          price: stopLoss,
          color: "#ff6b72",
          lineWidth: 1,
          lineStyle: LineStyle.Dotted,
          axisLabelVisible: true,
          title: position.order_source === "strategy_auto" ? "AUTO STOP LOSS" : "MANUAL 停損",
        }));
      }
      if (position.take_profit_price != null) {
        priceLinesRef.current.push(series.createPriceLine({
          price: position.take_profit_price,
          color: "#38bdf8",
          lineWidth: 1,
          lineStyle: LineStyle.Dotted,
          axisLabelVisible: true,
          title: "AUTO TAKE PROFIT",
        }));
      }
    }
    return () => {
      priceLinesRef.current.forEach(line => series.removePriceLine(line));
      priceLinesRef.current = [];
    };
  }, [candleRef, contract, snapshot.orders, snapshot.positions, symbol]);

  return fillMarkers;
}
