"use client";
import { useEffect, useState } from "react";
import { parameterDraft, parseParameterDraft, type PublicStrategy } from "../lib/strategy-catalog";

const apiBase = () => (process.env.NEXT_PUBLIC_MARKET_API_URL ?? (typeof window === "undefined" ? "" : window.location.origin)).replace(/\/$/, "");

export default function StrategyManager() {
  const [items, setItems] = useState<PublicStrategy[]>([]);
  const [drafts, setDrafts] = useState<Record<string, Record<string, string>>>({});
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    void fetch(`${apiBase()}/api/strategies`, { cache: "no-store" }).then(async response => {
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail ?? "無法讀取策略");
      if (active) {
        setItems(body.strategies);
        setDrafts(Object.fromEntries(body.strategies.map((item: PublicStrategy) => [item.key, parameterDraft(item)])));
        setLoaded(true);
      }
    }).catch(reason => { if (active) { setError(String(reason)); setLoaded(true); } });
    return () => { active = false; };
  }, []);
  const save = async (item: PublicStrategy) => {
    setBusy(item.key); setError(""); setNotice("");
    try {
      const parameters = parseParameterDraft(item, drafts[item.key] ?? {});
      const response = await fetch(`${apiBase()}/api/strategies/${encodeURIComponent(item.key)}`, {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ parameters }),
      });
      const saved = await response.json();
      if (!response.ok) throw new Error(saved.detail ?? "參數儲存失敗");
      setItems(current => current.map(value => value.key === item.key ? saved : value));
      setDrafts(current => ({ ...current, [item.key]: parameterDraft(saved) }));
      setNotice(`${item.name} 已儲存。`);
    } catch (reason) { setError(String(reason)); }
    finally { setBusy(""); }
  };
  if (!loaded) return <section className="panel strategy-loading">正在讀取策略參數…</section>;
  return <>
    {error && <div className="live-error">{error}</div>}
    {notice && <div className="strategy-notice">{notice}</div>}
    {!items.length && <section className="panel">目前沒有可用的策略。</section>}
    <section className="strategy-catalog editable">{items.map(item => <article className="panel" key={item.key}>
      <h2>{item.name}</h2>
      <div className="strategy-fields">{item.parameter_schema.fields.map(field => <label key={field.name}>
        <span>{field.title ?? field.name}<small>{field.description}</small></span>
        {field.kind === "boolean" ? <select aria-label={field.title ?? field.name} value={drafts[item.key]?.[field.name] ?? ""}
          onChange={event => setDrafts(current => ({ ...current, [item.key]: { ...current[item.key], [field.name]: event.target.value } }))}>
          <option value="">請選擇</option><option value="true">是</option><option value="false">否</option>
        </select> : <input aria-label={field.title ?? field.name} type={field.kind === "string" ? "text" : "number"}
          required={field.required} step={field.kind === "integer" ? 1 : "any"} value={drafts[item.key]?.[field.name] ?? ""}
          onChange={event => setDrafts(current => ({ ...current, [item.key]: { ...current[item.key], [field.name]: event.target.value } }))} />}
      </label>)}</div>
      <div className="strategy-actions"><button className="secondary" onClick={() => setDrafts(current => ({ ...current, [item.key]: parameterDraft(item) }))}>取消修改</button>
        <button disabled={busy === item.key} onClick={() => void save(item)}>{busy === item.key ? "儲存中…" : "儲存參數"}</button></div>
    </article>)}</section>
  </>;
}
