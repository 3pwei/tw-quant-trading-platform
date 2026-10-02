import type { Metadata } from "next";
import { CurrentUserProvider } from "./components/current-user-context";
import "./globals.css";

export const metadata: Metadata = {
  metadataBase: new URL(
    process.env.NEXT_PUBLIC_SITE_URL ??
      "https://platform.example.com/"
  ),
  title: "微型臺指期貨量化儀表板",
  description: "TMF 即時行情、模擬交易、歷史回測、策略與系統狀態儀表板。",
  openGraph: {
    title: "微型臺指期貨 1 分 K 與策略回測",
    description: "TMF 即時行情、歷史回測、基本與組合策略及系統狀態一站管理。",
    url: ".",
    siteName: "Trading Platform",
    locale: "zh_TW",
    type: "website",
  },
  twitter: {
    card: "summary",
    title: "微型臺指期貨 1 分 K 與策略回測",
    description: "TMF 即時行情、歷史回測、基本與組合策略及系統狀態一站管理。",
  },
  icons: {
    icon: "favicon.svg",
    shortcut: "favicon.svg",
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-Hant" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: `try{if(sessionStorage.getItem("tmf-demo-nav-authenticated")==="1")document.documentElement.dataset.demoNavReturning="true"}catch{}` }} />
      </head>
      <body>
        <CurrentUserProvider>{children}</CurrentUserProvider>
      </body>
    </html>
  );
}
