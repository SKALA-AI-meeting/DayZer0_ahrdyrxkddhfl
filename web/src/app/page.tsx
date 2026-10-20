"use client";
// 첫 화면: 채용 담당자 / 지원자 입구. 지원자는 가입 없이 초대 링크(또는 코드)로 들어온다(PRD 7.1, F-10).
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import Nav from "@/components/Nav";

export default function Home() {
  const router = useRouter();
  const [invite, setInvite] = useState("");
  const [error, setError] = useState("");

  function enter(e: React.FormEvent) {
    e.preventDefault();
    // 전체 링크를 붙여 넣어도, 끝의 코드만 넣어도 된다
    const code = invite.trim().split("/invite/").pop()?.split(/[?#]/)[0]?.replace(/\/$/, "");
    if (!code) { setError("초대 링크나 코드를 붙여 넣어 주세요."); return; }
    router.push(`/invite/${code}`);
  }

  return (
    <>
      <Nav />
      <main className="page">
        <section className="hero">
          <h1>입사 첫날 전에, 함께 일해 보는 하루</h1>
          <p>AI 동료와 가상 리모트 업무를 하며 드러나는 협업 행동을 근거 리포트로 전달합니다.</p>
        </section>
        <div className="grid2">
          <div className="card role-card">
            <h2><span className="icon">HR</span>채용 담당자</h2>
            <p className="muted">지원자를 초대하고, 세션이 끝나면 관찰 리포트를 확인합니다. 최종 판단은 담당자가 합니다.</p>
            <div className="row">
              <Link className="btn" href="/admin/login">로그인</Link>
              <Link className="btn secondary" href="/admin/login?mode=signup">회사 계정 만들기</Link>
            </div>
          </div>
          <form className="card role-card" onSubmit={enter}>
            <h2><span className="icon">IN</span>지원자</h2>
            <p className="muted">가입은 필요 없어요. 채용 담당자에게 받은 <b>초대 링크</b>를 열거나, 아래에 붙여 넣으세요.</p>
            <div className="row">
              <input placeholder="초대 링크 또는 코드" value={invite} onChange={(e) => setInvite(e.target.value)} />
              <button type="submit">입장</button>
            </div>
            {error && <div className="error" style={{ marginTop: 10 }}>{error}</div>}
          </form>
        </div>
      </main>
    </>
  );
}
