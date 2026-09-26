import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "智能客服",
  description: "多角色电商智能客服系统",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
