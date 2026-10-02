"use client";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";

const apiBase = () => (process.env.NEXT_PUBLIC_MARKET_API_URL ?? (typeof window === "undefined" ? "" : window.location.origin)).replace(/\/$/, "");

export default function CompositeBuilder({ mode }: { mode: "new" | "edit" }) {
  const router = useRouter();
  const search = useSearchParams();
  const requestedId = mode === "edit" ? search.get("strategy_id") : search.get("source_id");
  const version = mode === "new" ? search.get("version") : null;
  const [draft, setDraft] = useState("");
  const [name, setName] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let active = true;
    const load = async () => {
      if (mode === "edit" && !requestedId) throw new Error("缺少組合策略 ID");
      const path = requestedId ? `/api/composite-strategies/${encodeURIComponent(requestedId)}${version ? `?version=${encodeURIComponent(version)}` : ""}` : "/api/composite-strategies";
      const response = await fetch(apiBase() + path, { cache: "no-store" });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail ?? "組合策略目前無法使用");
      const definition = requestedId ? body.definition : body.template;
      if (active) { setName(definition.name ?? ""); setDraft(JSON.stringify(definition, null, 2)); }
    };
    void load().catch(reason => { if (active) setError(String(reason)); });
    return () => { active = false; };
  }, [mode, requestedId, version]);
  const save = async () => {
    setBusy(true); setError("");
    try {
      const definition = JSON.parse(draft);
      if (!definition || typeof definition !== "object" || Array.isArray(definition)) throw new Error("請輸入有效的組合設定");
      definition.name = name;
      const path = mode === "edit" && requestedId ? `/api/composite-strategies/${encodeURIComponent(requestedId)}` : "/api/composite-strategies";
      const response = await fetch(apiBase() + path, { method: mode === "edit" ? "PUT" : "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ definition }) });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail ?? "儲存失敗");
      router.push("/composite-strategies/");
    } catch (reason) { setError(String(reason)); }
    finally { setBusy(false); }
  };
  return <section className="panel">
    {error && <div className="live-error">{error}</div>}
    {draft && <><label>策略名稱<input value={name} onChange={event => setName(event.target.value)} /></label>
      <label>組合設定<textarea aria-label="組合設定" rows={20} value={draft} onChange={event => setDraft(event.target.value)} /></label>
      <p>使用已提供的策略版本、參數與成員設定。儲存前會驗證目前可用的策略。</p>
      <button disabled={busy} onClick={() => void save()}>{busy ? "儲存中…" : "儲存"}</button></>}
    <Link href="/composite-strategies/">返回組合策略</Link>
  </section>;
}
