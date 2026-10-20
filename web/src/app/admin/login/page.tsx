"use client";
// 기업 관리자 가입·로그인 (F-01)
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import Nav from "@/components/Nav";
import { adminToken, api } from "@/lib/api";

function LoginForm() {
  const router = useRouter();
  const params = useSearchParams();
  const [mode, setMode] = useState<"login" | "signup">(params.get("mode") === "signup" ? "signup" : "login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [company, setCompany] = useState("");
  const [scenarios, setScenarios] = useState<{ id: string; company: string; task_title: string }[]>([]);
  const [scenario, setScenario] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {   // 가입할 때 회사의 업무 시나리오를 한 번 고른다(같은 회사 지원자는 같은 조건, PRD 5.5)
    if (mode === "signup" && scenarios.length === 0)
      api<{ id: string; company: string; task_title: string }[]>("/scenarios")
        .then((list) => { setScenarios(list); setScenario((s) => s || list[0]?.id || ""); })
        .catch((e) => setError((e as Error).message));
  }, [mode, scenarios.length]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    try {
      const body = mode === "login" ? { email, password } : { email, password, company, scenario };
      const { token } = await api<{ token: string }>(`/auth/${mode}`, { method: "POST", body: JSON.stringify(body) });
      adminToken.set(token);
      router.push("/admin");
    } catch (err) {
      setError((err as Error).message);
    }
  }

  return (
    <main className="page narrow">
      <div className="card">
        <div className="tabs" style={{ marginBottom: 18 }}>
          <button type="button" className={mode === "login" ? "active" : ""} onClick={() => setMode("login")}>로그인</button>
          <button type="button" className={mode === "signup" ? "active" : ""} onClick={() => setMode("signup")}>회사 계정 만들기</button>
        </div>
        <h1 style={{ fontSize: 22 }}>{mode === "login" ? "채용 담당자 로그인" : "회사 계정 만들기"}</h1>
        <p className="muted small">지원자는 가입하지 않아요. 담당자가 만든 초대 링크로 입장합니다.</p>
        <form onSubmit={submit} className="stack">
          {mode === "signup" && <input placeholder="회사 이름 (예: 루프랩)" value={company} onChange={(e) => setCompany(e.target.value)} />}
          {mode === "signup" && (
            <label className="small muted">
              업무 시나리오 (가입 뒤에는 바꿀 수 없어요. 모든 지원자가 이 시나리오로 평가받습니다)
              <select value={scenario} onChange={(e) => setScenario(e.target.value)} style={{ width: "100%", marginTop: 4 }}>
                {scenarios.map((s, i) => (
                  <option key={s.id} value={s.id}>{s.company} · {s.task_title}{i === 0 ? " (기본)" : ""}</option>
                ))}
              </select>
            </label>
          )}
          <input placeholder="업무용 이메일" value={email} onChange={(e) => setEmail(e.target.value)} />
          <input placeholder="비밀번호 (8자 이상)" type="password" value={password} onChange={(e) => setPassword(e.target.value)} />
          {error && <div className="error">{error}</div>}
          <button type="submit">{mode === "login" ? "로그인" : "가입하고 시작하기"}</button>
        </form>
      </div>
    </main>
  );
}

export default function AdminLogin() {
  return (
    <>
      <Nav />
      <Suspense><LoginForm /></Suspense>
    </>
  );
}
