import type { StrategyVisualization } from "./trade-chart-model";

export function StrategyParameterSummary({
  visualizations,
}: {
  visualizations: StrategyVisualization[];
}) {
  if (!visualizations.length) return null;
  return <details className="strategy-parameter-summary" open>
    <summary>策略參數與門檻</summary>
    <div className="strategy-parameter-groups">
      {visualizations.map(item => <section key={item.strategy.key}>
        <h3>{item.strategy.name}</h3>
        <dl>{item.parameters.map(parameter => <div key={parameter.key} className={parameter.important ? "important" : ""}>
          <dt>{parameter.label}</dt>
          <dd>{String(parameter.display_value)}{parameter.unit ? ` ${parameter.unit}` : ""}</dd>
        </div>)}</dl>
      </section>)}
    </div>
  </details>;
}

export function StrategySeriesLegend({
  visualizations,
}: {
  visualizations: StrategyVisualization[];
}) {
  const series = visualizations.flatMap(item => [
    ...item.overlays.map(value => ({ ...value, strategy: item.strategy.name })),
    ...item.diagnostics.map(value => ({ ...value, strategy: item.strategy.name })),
  ]);
  if (!series.length) return null;
  return <div className="strategy-series-legend" aria-label="策略圖層圖例">
    {series.map(item => <span key={`${item.strategy}:${item.panel}:${item.key}`}>
      <i style={{ background: item.color ?? "#94a3b8" }} />
      {item.strategy} · {item.label}
    </span>)}
  </div>;
}
