import type { DemoResult } from "./demo-client";

export default function DemoEquityChart({ points }: { points: DemoResult["equity"] }) {
  if (!points.length) return <p>沒有權益資料。</p>;
  const width = 800, height = 220, left = 70, right = 18, top = 16, bottom = 32;
  const values = points.map(point => point.equity);
  const min = Math.min(...values), max = Math.max(...values);
  const pad = Math.max((max - min) * .15, 100);
  const low = min - pad, high = max + pad;
  const x = (index: number) => left + index / Math.max(points.length - 1, 1) * (width - left - right);
  const y = (value: number) => top + (high - value) / (high - low) * (height - top - bottom);
  const money = new Intl.NumberFormat("zh-TW", { maximumFractionDigits: 0 });
  return <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="合成案例累積權益與逐筆盈虧節奏">
    {[0, 1, 2].map(index => {
      const value = low + (high - low) * index / 2;
      return <g key={index}><line x1={left} x2={width - right} y1={y(value)} y2={y(value)} className="grid-line" />
        <text x={left - 8} y={y(value) + 4} textAnchor="end" className="axis">{money.format(value)}</text></g>;
    })}
    <path d={points.map((point, index) => `${index ? "L" : "M"}${x(index)} ${y(point.equity)}`).join(" ")} className="equity-line" fill="none" />
    {points.map((point, index) => <circle key={`${point.timestamp}-${index}`} cx={x(index)} cy={y(point.equity)} r="4" className={point.net_pnl >= 0 ? "eq-win" : "eq-loss"}>
      <title>{`${point.timestamp.slice(0, 16).replace("T", " ")} · 權益 NT$ ${money.format(point.equity)} · 本筆 ${money.format(point.net_pnl)}`}</title>
    </circle>)}
    <text x={left} y={height - 7} className="axis">{points[0].timestamp.slice(0, 10)}</text>
    <text x={width - right} y={height - 7} textAnchor="end" className="axis">{points.at(-1)?.timestamp.slice(0, 10)}</text>
  </svg>;
}
