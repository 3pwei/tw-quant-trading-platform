"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

type Definition = { description?: string; evaluator: { strategy: string }; members: unknown[] };
type Composite = { id: string; version: number; name: string; definition: Definition; created_at: string; archived_at?: string };
type Catalog = { strategies: Composite[]; archived_strategies?: Composite[] };

const apiBase = () => (process.env.NEXT_PUBLIC_MARKET_API_URL ?? (typeof window === "undefined" ? "" : window.location.origin)).replace(/\/$/, "");
const ruleStats = (definition: Definition) => ({ total: definition.members.length });

export default function CompositeCatalog() {
  const [activeItems, setActiveItems] = useState<Composite[]>([]);
  const [archivedItems, setArchivedItems] = useState<Composite[]>([]);
  const [tab, setTab] = useState<"active" | "archived">("active");
  const [query, setQuery] = useState("");
  const [historyId, setHistoryId] = useState<string | null>(null);
  const [versions, setVersions] = useState<Composite[]>([]);
  const [selectedArchived, setSelectedArchived] = useState<string[]>([]);
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");

  const load = async () => {
    const response = await fetch(`${apiBase()}/api/composite-strategies`, { cache: "no-store" });
    const body: Catalog & { detail?: string } = await response.json();
    if (!response.ok) throw new Error(body.detail ?? "無法取得組合策略");
    setActiveItems(body.strategies);
    setArchivedItems(body.archived_strategies ?? []);
  };

  // Initial remote hydration runs once; later refreshes are user-triggered.
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => { void load().catch(reason => setError(reason instanceof Error ? reason.message : "無法取得組合策略")); }, []);
  const items = useMemo(() => {
    const source = tab === "active" ? activeItems : archivedItems;
    const keyword = query.trim().toLocaleLowerCase("zh-TW");
    return keyword ? source.filter(item => `${item.name} ${item.definition.description}`.toLocaleLowerCase("zh-TW").includes(keyword)) : source;
  }, [activeItems, archivedItems, query, tab]);

  const toggleHistory = async (item: Composite) => {
    if (historyId === item.id) { setHistoryId(null); setVersions([]); return; }
    setError("");
    try {
      const response = await fetch(`${apiBase()}/api/composite-strategies/${item.id}/versions`, { cache: "no-store" });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail ?? "無法取得版本紀錄");
      setHistoryId(item.id); setVersions(body.versions);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "無法取得版本紀錄"); }
  };

  const archive = async (item: Composite) => {
    if (!window.confirm(`確定封存「${item.name}」？\n\n策略會從回測選單移除，但所有版本都會保留。`)) return;
    setBusy(item.id); setError(""); setNotice("");
    try {
      const response = await fetch(`${apiBase()}/api/composite-strategies/${item.id}`, { method: "DELETE" });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail ?? "策略封存失敗");
      setHistoryId(null); setVersions([]); await load(); setNotice(`${item.name} 已移至封存庫。`);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "策略封存失敗"); }
    finally { setBusy(""); }
  };

  const toggleArchived = (id: string) => setSelectedArchived(current => current.includes(id) ? current.filter(item => item !== id) : [...current, id]);
  const purge = async () => {
    if (!selectedArchived.length) return;
    const confirmation = window.prompt(`將永久刪除 ${selectedArchived.length} 個策略及其全部版本。\n此操作無法復原。\n\n請輸入「永久刪除」確認：`);
    if (confirmation !== "永久刪除") return;
    setBusy("purge"); setError(""); setNotice("");
    try {
      const response = await fetch(`${apiBase()}/api/composite-strategies/purge`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ strategy_ids: selectedArchived }) });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail ?? "永久刪除失敗");
      setSelectedArchived([]); setHistoryId(null); setVersions([]); await load(); setNotice(`已永久刪除 ${body.deleted_strategies} 個策略及 ${body.deleted_versions} 個版本。`);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "永久刪除失敗"); }
    finally { setBusy(""); }
  };

  return <>
    <section className="composite-catalog-toolbar panel">
      <div className="composite-status-tabs" role="tablist" aria-label="組合策略狀態">
        <button type="button" role="tab" aria-selected={tab === "active"} className={tab === "active" ? "active" : undefined} onClick={() => setTab("active")}>啟用中 <b>{activeItems.length}</b></button>
        <button type="button" role="tab" aria-selected={tab === "archived"} className={tab === "archived" ? "active" : undefined} onClick={() => setTab("archived")}>已封存 <b>{archivedItems.length}</b></button>
      </div>
      <input aria-label="搜尋組合策略" placeholder="搜尋策略名稱或說明" value={query} onChange={event => setQuery(event.target.value)} />
      <Link href="/composite-strategies/new/">＋ 建立組合策略</Link>
    </section>
    {error && <div className="live-error">{error}</div>}{notice && <div className="strategy-notice">{notice}</div>}
    {tab === "archived" && archivedItems.length > 0 && <div className="composite-archive-toolbar"><label><input type="checkbox" checked={selectedArchived.length === archivedItems.length} onChange={() => setSelectedArchived(selectedArchived.length === archivedItems.length ? [] : archivedItems.map(item => item.id))} /> 全選封存策略</label><button type="button" disabled={!selectedArchived.length || busy === "purge"} onClick={() => void purge()}>{busy === "purge" ? "刪除中…" : `永久刪除（${selectedArchived.length}）`}</button></div>}
    <section className="composite-catalog-grid">
      {items.map(item => {
        const stats = ruleStats(item.definition);
        const archived = tab === "archived";
        return <article className="panel composite-card" key={item.id}>
          <header><div>{archived && <input aria-label={`選取 ${item.name}`} type="checkbox" checked={selectedArchived.includes(item.id)} onChange={() => toggleArchived(item.id)} />}<span>{archived ? "ARCHIVED" : "ACTIVE"}</span><b>v{item.version}</b></div><h2>{item.name}</h2><p>{item.definition.description || "尚未填寫策略說明"}</p></header>
          <dl><div><dt>執行策略</dt><dd>{item.definition.evaluator.strategy}</dd></div><div><dt>條件數量</dt><dd>{stats.total} 個</dd></div><div><dt>最後更新</dt><dd>{new Date(item.created_at).toLocaleDateString("zh-TW", { timeZone: "Asia/Taipei" })}</dd></div></dl>
          <div className="composite-card-actions">
            {archived ? <Link href={`/composite-strategies/new/?source_id=${item.id}&version=${item.version}`}>複製為新策略</Link> : <Link className="primary" href={`/composite-strategies/editor/?strategy_id=${item.id}`}>編輯</Link>}
            <button type="button" onClick={() => void toggleHistory(item)}>{historyId === item.id ? "收合版本" : "版本紀錄"}</button>
            {!archived && <Link href="/backtest/">執行回測</Link>}
            {!archived && <button type="button" className="danger" disabled={busy === item.id} onClick={() => void archive(item)}>{busy === item.id ? "封存中…" : "封存"}</button>}
          </div>
          {historyId === item.id && <div className="composite-version-history">{versions.map(version => <div key={version.version}><span><b>v{version.version}</b><small>{new Date(version.created_at).toLocaleString("zh-TW", { timeZone: "Asia/Taipei" })}</small></span><Link href={`/composite-strategies/new/?source_id=${item.id}&version=${version.version}`}>複製</Link></div>)}</div>}
        </article>;
      })}
    </section>
    {!items.length && <section className="panel composite-empty"><b>{query ? "找不到符合的策略" : tab === "active" ? "尚未建立組合策略" : "封存庫目前是空的"}</b><p>{tab === "active" && !query ? "以目前可用的策略與成員設定建立組合。" : "可以切換分類或調整搜尋條件。"}</p>{tab === "active" && !query && <Link href="/composite-strategies/new/">建立第一個組合策略</Link>}</section>}
  </>;
}
