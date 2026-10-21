"use client";
// 첫 화면: 왼쪽은 서비스 소개(진행 방식 · 업무 보드 미리보기), 오른쪽은 입장 창(채용 담당자 / 지원자 탭).
// 지원자는 가입 없이 초대 링크(또는 코드)로 들어온다(PRD 7.1, F-10).
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import Nav from "@/components/Nav";

type Role = "candidate" | "admin";

// 업무 보드 미리보기(실제 시나리오 내용이 아닌 예시). 어떤 상황이 연출되는지는 보여 주지 않는다.
const PREVIEW = [
  { n: "01", title: "자료 조사", owner: "나", done: 1, units: 2 },
  { n: "02", title: "인터뷰 정리", owner: "동료", done: 0, units: 2 },
  { n: "03", title: "보고 자료", owner: "나", done: 0, units: 3 },
];

export default function Home() {
  const router = useRouter();
  const [role, setRole] = useState<Role>("candidate");
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
      <main className="home">
        <section className="home-intro">
          <p className="home-kicker">Day One보다 하루 먼저</p>
          <h1>같이 일해 보면<br />보이는 것들</h1>
          <p className="home-lead">
            지원자는 40분 동안 AI 팀장·동료와 한 프로젝트를 맡아 일합니다.
            채용 담당자는 그 과정에서 드러난 협업 태도를 원문 근거와 함께 받습니다.
          </p>

          <ol className="home-steps">
            <li><b>초대 링크로 입장</b><span>가입 없이, 담당자가 보낸 링크 하나로</span></li>
            <li><b>40분 가상 업무</b><span>메신저로 대화하고 업무 카드를 골라 진행</span></li>
            <li><b>근거 리포트</b><span>역량 5개, 원문·시각과 참고 등급</span></li>
          </ol>

          <div className="home-preview" aria-hidden="true">
            {PREVIEW.map((c) => (
              <div key={c.n} className="mini-card">
                <div className="mini-top"><span className="tnum">{c.n}</span><span className={`owner ${c.owner === "나" ? "candidate" : "coworker"}`}>{c.owner}</span></div>
                <b>{c.title}</b>
                <span className="cells">{Array.from({ length: c.units }, (_, i) => <i key={i} className={i < c.done ? "on" : ""} />)}</span>
              </div>
            ))}
          </div>
        </section>

        <section className="home-door">
          <div className="door-tabs" role="tablist">
            <button role="tab" aria-selected={role === "candidate"} className={role === "candidate" ? "active" : ""}
              onClick={() => setRole("candidate")}>지원자</button>
            <button role="tab" aria-selected={role === "admin"} className={role === "admin" ? "active" : ""}
              onClick={() => setRole("admin")}>채용 담당자</button>
          </div>

          {role === "candidate" ? (
            <form className="door-body" onSubmit={enter}>
              <h2>업무 시작하기</h2>
              <p className="muted">채용 담당자에게 받은 <b>초대 링크</b>를 열거나, 아래에 붙여 넣으세요. 가입은 필요 없어요.</p>
              <input placeholder="초대 링크 또는 코드" value={invite} onChange={(e) => setInvite(e.target.value)} />
              <button type="submit">입장</button>
              {error && <div className="error">{error}</div>}
            </form>
          ) : (
            <div className="door-body">
              <h2>지원자 초대하기</h2>
              <p className="muted">지원자를 초대하고, 세션이 끝나면 관찰 리포트를 확인합니다. 최종 판단은 담당자가 합니다.</p>
              <Link className="btn" href="/admin/login">로그인</Link>
              <Link className="btn secondary" href="/admin/login?mode=signup">회사 계정 만들기</Link>
            </div>
          )}
        </section>
      </main>
    </>
  );
}
