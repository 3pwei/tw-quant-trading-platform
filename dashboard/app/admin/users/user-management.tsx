"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";

type Role = "researcher" | "trader" | "admin";
type Status = "active" | "suspended" | "revoked";
type TradingMode = "disabled" | "paper" | "live";
type User = {
  user_id: string;
  email: string;
  role: Role;
  status: Status;
  trading_mode: TradingMode;
  registered: boolean;
  identity_bound: boolean;
};
type AccessRequest = {
  request_id: string;
  email: string;
  status: "pending" | "approved" | "rejected";
  requested_at: string;
  updated_at: string;
};

function formatTime(value: string) {
  return new Intl.DateTimeFormat("zh-TW", {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: "Asia/Taipei",
  }).format(new Date(value));
}

export default function UserManagement() {
  const [users, setUsers] = useState<User[]>([]);
  const [requests, setRequests] = useState<AccessRequest[]>([]);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<Role>("researcher");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    const [usersResponse, requestsResponse] = await Promise.all([
      fetch("/api/admin/users", { cache: "no-store" }),
      fetch("/api/admin/access-requests", { cache: "no-store" }),
    ]);
    if (!usersResponse.ok || !requestsResponse.ok) {
      throw new Error("沒有權限讀取帳號，或服務暫時不可用");
    }
    const usersBody: { users: User[] } = await usersResponse.json();
    const requestsBody: { requests: AccessRequest[] } =
      await requestsResponse.json();
    setUsers(usersBody.users);
    setRequests(requestsBody.requests);
  }, []);

  useEffect(() => {
    const initial = window.setTimeout(() => {
      void load().catch(reason =>
        setError(reason instanceof Error ? reason.message : "讀取失敗")
      );
    }, 0);
    return () => window.clearTimeout(initial);
  }, [load]);

  async function createUser(event: FormEvent) {
    event.preventDefault();
    setBusy("create");
    setError("");
    setNotice("");
    try {
      const response = await fetch("/api/admin/users", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email,
          role,
          status: "active",
          trading_mode: "disabled",
        }),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail ?? "新增失敗");
      setEmail("");
      setNotice(`已新增 ${body.email}`);
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "新增失敗");
    } finally {
      setBusy(null);
    }
  }

  async function reviewRequest(
    accessRequest: AccessRequest,
    decision: "approve" | "reject"
  ) {
    setBusy(accessRequest.request_id);
    setError("");
    setNotice("");
    try {
      const response = await fetch(
        `/api/admin/access-requests/${accessRequest.request_id}/${decision}`,
        { method: "POST" }
      );
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail ?? "審核失敗");
      setNotice(
        decision === "approve"
          ? `已核准 ${accessRequest.email}，角色為研究／回測`
          : `已拒絕 ${accessRequest.email}`
      );
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "審核失敗");
    } finally {
      setBusy(null);
    }
  }

  async function updateUser(user: User, changes: Partial<User>) {
    const next = { ...user, ...changes };
    const tradingMode = next.role === "researcher"
      ? "disabled"
      : next.role === "admin" && next.trading_mode === "live"
        ? "disabled"
        : next.trading_mode;
    setBusy(user.user_id);
    setError("");
    setNotice("");
    try {
      const response = await fetch(`/api/admin/users/${user.user_id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          role: next.role,
          status: next.status,
          trading_mode: tradingMode,
        }),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail ?? "更新失敗");
      setNotice(`已更新 ${body.email}`);
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "更新失敗");
    } finally {
      setBusy(null);
    }
  }

  return (
    <>
      <section className="access-requests panel">
        <div className="panel-head">
          <div>
            <span>PENDING ACCESS</span>
            <h2>待審核申請</h2>
          </div>
          <small>{requests.length} 筆待處理</small>
        </div>
        {requests.length === 0 ? (
          <p className="access-request-empty">目前沒有待審核申請。</p>
        ) : (
          <div className="access-request-list">
            {requests.map(accessRequest => (
              <article key={accessRequest.request_id}>
                <div>
                  <strong>{accessRequest.email}</strong>
                  <time dateTime={accessRequest.requested_at}>
                    申請時間：{formatTime(accessRequest.requested_at)}
                  </time>
                </div>
                <div className="access-request-actions">
                  <button
                    className="approve"
                    disabled={busy !== null}
                    onClick={() =>
                      void reviewRequest(accessRequest, "approve")
                    }
                  >
                    {busy === accessRequest.request_id
                      ? "處理中"
                      : "核准為研究帳號"}
                  </button>
                  <button
                    className="reject"
                    disabled={busy !== null}
                    onClick={() =>
                      void reviewRequest(accessRequest, "reject")
                    }
                  >
                    拒絕
                  </button>
                </div>
              </article>
            ))}
          </div>
        )}
        <p className="admin-footnote">
          核准後會建立 Researcher 帳號並停用交易功能；需要其他角色時，可在下方帳號清單調整。
        </p>
      </section>

      <form className="admin-user-create panel" onSubmit={createUser}>
        <div>
          <span>DIRECT APPROVAL</span>
          <strong>也可以直接新增已知 Email，首次登入時會綁定驗證身分。</strong>
        </div>
        <input
          type="email"
          required
          value={email}
          onChange={event => setEmail(event.target.value)}
          placeholder="user@example.com"
          aria-label="Email"
        />
        <select
          value={role}
          onChange={event => setRole(event.target.value as Role)}
          aria-label="角色"
        >
          <option value="researcher">研究／回測</option>
          <option value="trader">交易者</option>
          <option value="admin">系統管理員</option>
        </select>
        <button disabled={busy !== null}>
          {busy === "create" ? "處理中" : "新增帳號"}
        </button>
      </form>

      {error && <div className="admin-message error">{error}</div>}
      {notice && <div className="admin-message success">{notice}</div>}

      <section className="admin-users panel">
        <div className="panel-head">
          <div>
            <span>AUTHORIZED USERS</span>
            <h2>平台帳號</h2>
          </div>
          <small>{users.length} 個帳號</small>
        </div>
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Email</th>
                <th>角色</th>
                <th>帳號狀態</th>
                <th>交易模式</th>
                <th>身分綁定</th>
              </tr>
            </thead>
            <tbody>
              {users.map(user => (
                <tr key={user.user_id}>
                  <td data-label="Email">{user.email}</td>
                  <td data-label="角色">
                    <select
                      disabled={busy !== null}
                      value={user.role}
                      onChange={event =>
                        void updateUser(user, {
                          role: event.target.value as Role,
                        })
                      }
                    >
                      <option value="researcher">研究／回測</option>
                      <option value="trader">交易者</option>
                      <option value="admin">系統管理員</option>
                    </select>
                  </td>
                  <td data-label="帳號狀態">
                    <select
                      disabled={busy !== null}
                      value={user.status}
                      onChange={event =>
                        void updateUser(user, {
                          status: event.target.value as Status,
                        })
                      }
                    >
                      <option value="active">啟用</option>
                      <option value="suspended">暫停</option>
                      <option value="revoked">撤銷</option>
                    </select>
                  </td>
                  <td data-label="交易模式">
                    <select
                      disabled={busy !== null || user.role === "researcher"}
                      value={user.trading_mode}
                      onChange={event =>
                        void updateUser(user, {
                          trading_mode: event.target.value as TradingMode,
                        })
                      }
                    >
                      <option value="disabled">不允許下單</option>
                      <option value="paper">模擬下單</option>
                      {user.role === "trader" && (
                        <option value="live">實盤下單</option>
                      )}
                    </select>
                  </td>
                  <td data-label="身分綁定">
                    <span
                      className={user.identity_bound ? "bound" : "pending"}
                    >
                      {user.identity_bound ? "已綁定" : "等待首次登入"}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="admin-footnote">
          Admin 預設不允許下單，但可明確啟用 Paper；只有 Trader 可選擇 live。此階段尚未提供任何真實下單 API。
        </p>
      </section>
    </>
  );
}
