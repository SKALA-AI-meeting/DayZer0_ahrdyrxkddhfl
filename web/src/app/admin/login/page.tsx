"use client";
// 기업 관리자 가입·로그인 (F-01)
import { useRouter } from "next/navigation";
import { useState } from "react";
import { adminToken, api } from "@/lib/api";

export default function AdminLogin() {
  const router = useRouter();
  const [mode, setMode] = useState<"login" | "signup">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [company, setCompany] = useState("");
  const [error, setError] = useState("");

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    try {
      const body = mode === "login" ? { email, password } : { email, password, company };
      const { token } = await api<{ token: string }>(`/auth/${mode}`, {
        method: "POST",
        body: JSON.stringify(body),
      });
      adminToken.set(token);
      router.push("/admin");
    } catch (err) {
      setError((err as Error).message);
    }
  }

  return (
    <main className="page" style={{ maxWidth: 420 }}>
      <h1>{mode === "login" ? "관리자 로그인" : "관리자 가입"}</h1>
      <form className="card" onSubmit={submit} style={{ display: "grid", gap: 8 }}>
        <input placeholder="이메일" value={email} onChange={(e) => setEmail(e.target.value)} />
        <input placeholder="비밀번호 (8자 이상)" type="password" value={password}
          onChange={(e) => setPassword(e.target.value)} />
        {mode === "signup" && (
          <input placeholder="회사 이름" value={company} onChange={(e) => setCompany(e.target.value)} />
        )}
        {error && <div className="error">{error}</div>}
        <button type="submit">{mode === "login" ? "로그인" : "가입하고 시작"}</button>
      </form>
      <button className="secondary" onClick={() => setMode(mode === "login" ? "signup" : "login")}>
        {mode === "login" ? "처음이신가요? 가입하기" : "이미 계정이 있어요"}
      </button>
    </main>
  );
}
