import type { ReactNode } from "react";
import SystemNav, { type SystemRoute } from "./system-nav";

export default function SectionShell({
  active,
  eyebrow,
  title,
  children,
}: {
  active: SystemRoute;
  eyebrow: string;
  title: string;
  children: ReactNode;
}) {
  return (
    <main className="portal-shell">
      <header className="portal-header">
        <div className="brand">
          <div><span>{eyebrow}</span><h1>{title}</h1></div>
        </div>
      </header>
      <SystemNav active={active} />
      {children}
    </main>
  );
}
