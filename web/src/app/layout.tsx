import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = { title: "Day Zero", description: "AI 동료 협업 시뮬레이션 채용 평가 (MVP)" };

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="ko">
      <body>{children}</body>
    </html>
  );
}
