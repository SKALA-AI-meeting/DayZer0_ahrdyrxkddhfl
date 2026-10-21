import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = { title: "Day Zero", description: "AI 동료 협업 시뮬레이션 채용 평가 (MVP)" };

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="ko">
      <head>
        {/* 본문 IBM Plex Sans KR, 시각·숫자 IBM Plex Mono (globals.css --sans·--mono) */}
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="" />
        <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans+KR:wght@400;500;600;700&display=swap" />
      </head>
      <body>{children}</body>
    </html>
  );
}
