"use client";

import Link from "next/link";
import { useCurrentUser } from "./current-user-context";
import { routeVisibility } from "./system-nav-policy";

export type SystemRoute =
  | "/"
  | "/trade/"
  | "/backtest/"
  | "/demo/"
  | "/replay/"
  | "/history/"
  | "/strategies/"
  | "/composite-strategies/"
  | "/settings/"
  | "/admin/users/";

type SystemNavRoute = {
  href: SystemRoute;
  label: string;
  permission?: string;
  admin?: boolean;
};

const routes: readonly SystemNavRoute[] = [
  { href: "/", label: "總覽" },
  { href: "/trade/", label: "交易工作台" },
  { href: "/backtest/", label: "歷史回測" },
  { href: "/demo/", label: "互動展示" },
  { href: "/replay/", label: "行情回放" },
  { href: "/history/", label: "執行紀錄" },
  { href: "/strategies/", label: "基本策略" },
  { href: "/composite-strategies/", label: "組合策略" },
  { href: "/settings/", label: "系統設定", admin: true },
  { href: "/admin/users/", label: "帳號權限", admin: true },
];

export default function SystemNav({ active }: { active: SystemRoute }) {
  const currentUser = useCurrentUser();

  return (
    <nav className="system-nav" aria-label="系統功能">
      <div className="system-nav-routes">
        {routes.map(route => {
          const visibility = routeVisibility(route, currentUser);
          if (visibility === "hidden") return null;
          if (visibility === "placeholder") {
            return (
              <span
                key={route.href}
                className="system-nav-placeholder"
                aria-hidden="true"
              >
                {route.label}
              </span>
            );
          }
          return (
            <Link
              key={route.href}
              href={route.href}
              className={route.href === active ? "active" : undefined}
              aria-current={route.href === active ? "page" : undefined}
            >
              {route.label}
            </Link>
          );
        })}
      </div>
      <div className="system-account">
        {currentUser.user && (
          <span title={currentUser.user.email}>
            {currentUser.user.email}
            <small>{currentUser.user.role}</small>
          </span>
        )}
        <a className="logout-link" href="/cdn-cgi/access/logout" onClick={() => { try { sessionStorage.removeItem("tmf-demo-nav-authenticated"); } catch { /* Storage may be unavailable. */ } }}>
          登出
        </a>
      </div>
      <details className="system-account-mobile">
        <summary aria-label="開啟帳號選單">帳號</summary>
        <div className="system-account-menu">
          {currentUser.user && (
            <span title={currentUser.user.email}>
              {currentUser.user.email}
              <small>{currentUser.user.role}</small>
            </span>
          )}
          <a className="logout-link" href="/cdn-cgi/access/logout" onClick={() => { try { sessionStorage.removeItem("tmf-demo-nav-authenticated"); } catch { /* Storage may be unavailable. */ } }}>
            登出
          </a>
        </div>
      </details>
    </nav>
  );
}
