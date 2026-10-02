"use client";

import Link from "next/link";
import { useEffect } from "react";

const basePath = process.env.NEXT_PUBLIC_BASE_PATH ?? "";
const tradePath = `${basePath}/trade/`;

export default function TradeRedirect() {
  useEffect(() => {
    window.location.replace(tradePath);
  }, []);

  return (
    <main className="state">
      <p>
        正在前往交易工作台… <Link className="live-link" href="/trade/">立即開啟</Link>
      </p>
    </main>
  );
}
