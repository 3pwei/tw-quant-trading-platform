import type { StrategySeries, StrategySeriesPoint } from "./trade-chart-model";

export type PreparedStrategySeries = {
  groupKey: string;
  definition: StrategySeries;
  points: StrategySeriesPoint[];
};

type StrategySeriesPreparation = {
  thresholdRange?: { from: string; to: string };
  includePoint?: (point: StrategySeriesPoint) => boolean;
};

/**
 * Prepare one generic strategy definition for a chart renderer.
 *
 * Grouped points become independent chart series so unrelated channel
 * segments cannot be joined. Threshold definitions remain a single series
 * whose endpoints follow the renderer's current visible range or cursor.
 */
export function prepareStrategySeries(
  definition: StrategySeries,
  options: StrategySeriesPreparation = {},
): PreparedStrategySeries[] {
  const thresholdValue = definition.metadata?.value;
  const sourcePoints = definition.type === "threshold"
    && typeof thresholdValue === "number"
    && options.thresholdRange
    ? [
        { time: options.thresholdRange.from, value: thresholdValue },
        { time: options.thresholdRange.to, value: thresholdValue },
      ]
    : definition.points;
  const groups = new Map<string, Map<string, StrategySeriesPoint>>();

  sourcePoints
    .filter(point => options.includePoint?.(point) ?? true)
    .forEach(point => {
      const groupKey = point.group ?? definition.key;
      const points = groups.get(groupKey) ?? new Map<string, StrategySeriesPoint>();
      points.set(point.time, point);
      groups.set(groupKey, points);
    });

  return [...groups.entries()].map(([groupKey, points]) => ({
    groupKey,
    definition,
    points: [...points.values()].sort(
      (left, right) => Date.parse(left.time) - Date.parse(right.time),
    ),
  }));
}
