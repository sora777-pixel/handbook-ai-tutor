import type { Metadata } from "next";
import { Inter } from "next/font/google";
import "./globals.css";
import { Providers } from "@/components/providers";
import { SessionHeartbeat } from "@/components/session-heartbeat";

const inter = Inter({
  subsets: ["latin"],
  display: "swap",
  variable: "--font-inter",
});

export const metadata: Metadata = {
  title: "Handbook AI Tutor · 上传一份资料，开始你的智能学习",
  description:
    "上传 PDF、MP4、EPUB 或图片，AI 自动做摘要、知识点梳理、出题、判分与对话讲解——所有调用都在本地/后端完成，浏览器永不接触 API KEY。",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN" className={inter.variable}>
      <body className="font-sans">
        <Providers>
          <SessionHeartbeat />
          {children}
        </Providers>
      </body>
    </html>
  );
}
