"use client";

import { useEffect, useRef, useState } from "react";
import InteractiveTradeChart from "../backtest/interactive-trade-chart";
import SystemNav from "../components/system-nav";
import { useCurrentUser } from "../components/current-user-context";
import { formatTaipeiClock } from "../lib/formatters";
import { formatDecimal, formatMoney, formatSignedMoney } from "../lib/formatters";
import { exitReasonLabel } from "../components/exit-reason";
import DemoEquityChart from "./demo-equity-chart";
import { demoErrorMessage, loadDemoCases, runDemoCase, type DemoCase, type DemoResult } from "./demo-client";

export default function DemoPage() {
  const currentUser = useCurrentUser();
  const [cases, setCases] = useState<DemoCase[]>([]);
  const [caseId, setCaseId] = useState("");
  const [result, setResult] = useState<DemoResult | null>(null);
  const [selected, setSelected] = useState(0);
  const [loadingCases, setLoadingCases] = useState(true);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState("");
  const inFlight = useRef(false);

  async function refreshCases() {
    setLoadingCases(true); setError("");
    try {
      const available = await loadDemoCases();
      setCases(available);
      setCaseId(current => available.some(item => item.id === current) ? current : available[0]?.id ?? "");
    } catch (cause) { setError(demoErrorMessage(cause)); }
    finally { setLoadingCases(false); }
  }
  useEffect(() => {
    let active = true;
    loadDemoCases().then(available => {
      if (!active) return;
      setCases(available); setCaseId(available[0]?.id ?? ""); setLoadingCases(false);
    }).catch(cause => {
      if (!active) return;
      setError(demoErrorMessage(cause)); setLoadingCases(false);
    });
    return () => { active = false; };
  }, []);

  async function run() {
    if (inFlight.current || !cases.some(item => item.id === caseId)) return;
    inFlight.current = true; setRunning(true); setError(""); setResult(null);
    try { setResult(await runDemoCase(caseId)); setSelected(0); }
    catch (cause) { setError(demoErrorMessage(cause)); }
    finally { inFlight.current = false; setRunning(false); }
  }

  const trade = result?.trades[selected];
  const summary = result?.summary;
  const worst = result?.trades.length ? Math.min(...result.trades.map(item => item.net_pnl)) : null;
  const best = result?.trades.length ? Math.max(...result.trades.map(item => item.net_pnl)) : null;
  const navPending = currentUser.status === "loading";
  const showNav = navPending || Boolean(currentUser.user);
  return <main className="portal-shell demo-page">
    <header className="portal-header"><div className="brand"><div><span>MILESPAPA QUANT LAB · DEMO</span><h1>互動策略分析展示</h1></div></div></header>
    <div className={`demo-nav-frame${navPending ? " pending" : ""}`} aria-hidden={navPending ? true : undefined}>
      {showNav && <SystemNav active="/demo/" />}
    </div>
    <section className="panel demo-intro">
      <div><span className="kicker">SYNTHETIC BACKTEST</span><h2>選擇固定案例，觀察策略訊號</h2>
        <p>選擇固定的合成策略案例，查看日期與 K 棒、模擬績效、風險及逐筆交易。策略參數、組合配方與內部診斷不會由公開 Demo 回傳。</p>
        <div className="tags"><span className="synthetic">合成資料</span><span className="synthetic">模擬交易</span><span className="synthetic">不代表實際績效</span></div>
      </div>
      <div className="demo-actions"><label htmlFor="demo-case">展示案例</label>
        <select id="demo-case" value={caseId} disabled={loadingCases || running || !cases.length} onChange={event => { setCaseId(event.target.value); setResult(null); }}>
          {cases.map(item => <option value={item.id} key={item.id}>{item.label}</option>)}
        </select>
        <button type="button" onClick={() => void run()} disabled={loadingCases || running || !caseId}>{running ? "正在執行…" : "執行示範回測"}</button>
      </div>
    </section>
    {error && <div className="live-error demo-status" role="alert">{error} <button type="button" disabled={running || loadingCases} onClick={() => void (cases.length ? run() : refreshCases())}>重試</button></div>}
    <section aria-live="polite" className="demo-status">
      {loadingCases ? "正在載入案例…" : running ? "正在計算合成案例，請稍候…" : !cases.length && !error ? "目前沒有可用的展示案例。" : !result && !error ? "選擇案例後按下執行，即可查看圖表與診斷。" : null}
    </section>
    {result && <>
      <section className="demo-case-meta" aria-label="合成案例資料範圍"><strong>{result.visualization.strategy.name}</strong><span>合成交易日：{result.date_range}</span><span>1 分 K · {result.bars.length} 根 · {result.trades.length} 筆模擬交易</span></section>
      {summary && <section className="metrics demo-metrics" aria-label="合成案例績效指標">
        {([
          ["淨利", formatSignedMoney(summary.net_profit), `交易成本 NT$ ${formatMoney(summary.total_cost)}`],
          ["總報酬", `${formatDecimal(summary.return_pct)}%`, `期末權益 NT$ ${formatMoney(summary.ending_equity)}`],
          ["最大回撤", `NT$ ${formatMoney(summary.max_drawdown)}`, `${formatDecimal(summary.max_drawdown_pct)}% of peak`],
          ["勝率", `${formatDecimal(summary.win_rate_pct)}%`, `${result.trades.length} 筆交易`],
          ["Profit Factor", summary.profit_factor == null ? "N/A" : formatDecimal(summary.profit_factor), "無虧損交易時不估計"],
          ["日頻 Sharpe", summary.daily_sharpe == null ? "N/A" : formatDecimal(summary.daily_sharpe), "僅三個合成交易日，參考性低"],
        ] as const).map(([label, value, note]) => <article className="metric" key={label}><span>{label}</span><strong>{value}</strong><small>{note}</small></article>)}
      </section>}
      <section className="panel demo-results"><div className="panel-head"><div><span>STRATEGY EXPLORER</span><h2>{result.visualization.strategy.name} · 價格與訊號</h2></div></div>
          <p>合成資料 {result.date_range}。圖表時間軸顯示日期與時間；模擬績效不代表真實或未來績效。策略設定僅供管理員在受保護介面檢視。</p>
        {trade ? <>
          <div className="demo-trade-list" aria-label="模擬交易選擇">{result.trades.map((item, index) => <button type="button" key={`${item.entry_time}-${index}`} className={index === selected ? "active" : ""} aria-pressed={index === selected} onClick={() => setSelected(index)}>
            #{index + 1} · {item.entry_time.slice(0, 10)} · {item.direction === "long" ? "做多" : "做空"} · {formatSignedMoney(item.net_pnl)}
          </button>)}</div>
          <InteractiveTradeChart key={result.case_id} bars={result.bars} trades={result.trades} selectedTrade={trade} visualization={result.visualization} overlays={result.overlays} showDate />
          <p className="demo-trade-text">第 {selected + 1} 筆：{trade.entry_time.slice(0, 10)} {formatTaipeiClock(trade.entry_time)} 進場 {formatDecimal(trade.entry_price)}；{trade.exit_time.slice(0, 10)} {formatTaipeiClock(trade.exit_time)} 出場 {formatDecimal(trade.exit_price)}。淨損益 {formatSignedMoney(trade.net_pnl)}。</p>
        </> : <p>這個案例有 K 棒，但沒有完整的模擬進出場交易。</p>}
      </section>
      <div className="analysis demo-analysis"><section className="panel equity-panel"><div className="panel-head"><div><span>EQUITY CURVE</span><h2>累積權益與虧損節奏</h2></div></div><div className="equity-chart"><DemoEquityChart points={result.equity} /></div><p>初始示範資金 NT$ {formatMoney(result.config.initial_capital)}；每個節點對應一筆交易的結算。僅供理解回測介面。</p></section>
        <aside className="panel risk-panel"><span className="kicker">RISK CHECK</span><h2>承擔風險</h2><dl>
          <div><dt>最大回撤</dt><dd className="loss">{summary ? `${formatDecimal(summary.max_drawdown_pct)}%` : "N/A"}</dd></div>
          <div><dt>單筆最大虧損</dt><dd className="loss">{worst == null ? "N/A" : formatSignedMoney(worst)}</dd></div>
          <div><dt>單筆最大獲利</dt><dd className="profit">{best == null ? "N/A" : formatSignedMoney(best)}</dd></div>
          <div><dt>策略設定</dt><dd>受保護</dd></div>
        </dl><div className="alert"><b>合成成本與樣本限制</b><p>每邊手續費 NT$ {formatMoney(result.config.commission_per_side)}、滑價 {result.config.slippage_points} 點；僅三個合成交易日，風險與 Sharpe 不可外推。</p></div></aside></div>
      <section className="panel ledger demo-ledger"><div className="panel-head"><div><span>TRADE LEDGER</span><h2>逐筆交易明細</h2></div><small>共 {result.trades.length} 筆</small></div><div className="table-scroll"><table><thead><tr><th>#</th><th>交易日</th><th>方向</th><th>進場</th><th>出場</th><th>停損／停利</th><th>成本</th><th>淨損益</th><th>出場原因</th></tr></thead><tbody>{result.trades.map((item, index) => <tr key={`${item.entry_time}-${index}`} className={selected === index ? "selected" : ""} onClick={() => setSelected(index)}><td>{index + 1}</td><td>{item.trading_date ?? item.entry_time.slice(0, 10)}</td><td>{item.direction === "long" ? "多" : "空"}</td><td>{formatTaipeiClock(item.entry_time)}<small>{formatDecimal(item.entry_price)}</small></td><td>{formatTaipeiClock(item.exit_time)}<small>{formatDecimal(item.exit_price)}</small></td><td>{item.stop_loss_price == null ? "—" : formatDecimal(item.stop_loss_price)}<small>{item.take_profit_price == null ? "—" : formatDecimal(item.take_profit_price)}</small></td><td>NT$ {formatMoney(item.total_cost)}</td><td className={item.net_pnl >= 0 ? "profit" : "loss"}>{formatSignedMoney(item.net_pnl)}</td><td>{exitReasonLabel(item.exit_reason ?? "")}</td></tr>)}</tbody></table></div></section>
    </>}
  </main>;
}
