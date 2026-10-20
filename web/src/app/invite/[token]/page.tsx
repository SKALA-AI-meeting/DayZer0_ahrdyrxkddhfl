"use client";
// 지원자 흐름: 링크 입장(F-10) → 안내·동의(F-11) → 시작 대기 → 가상 오피스 → 디브리핑(F-18)
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import Office from "@/components/Office";
import { api } from "@/lib/api";

type Info = { state: string; company: string; alias: string; duration_minutes: number;
  consented: boolean; started: boolean };
type Debrief = { events: { time: string; element: string; message: string }[]; notice: string };

export default function InvitePage() {
  const { token } = useParams<{ token: string }>();
  const [info, setInfo] = useState<Info | null>(null);
  const [step, setStep] = useState<"intro" | "ready" | "office" | "debrief">("intro");
  const [agree, setAgree] = useState(false);
  const [debrief, setDebrief] = useState<Debrief | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    api<Info>(`/invite/${token}`).then((i) => {
      setInfo(i);
      if (i.state === "completed") showDebrief();
      else if (i.started) setStep("office");          // 이어하기(F-19): 같은 링크로 다시 들어오면 그대로 이어감
      else if (i.consented) setStep("ready");
    }).catch((e) => setError(e.message));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  async function consent() {
    try {
      await api(`/invite/${token}/consent`, { method: "POST" });
      setStep("ready");
    } catch (e) { setError((e as Error).message); }
  }

  async function start() {
    try {
      await api(`/invite/${token}/start`, { method: "POST" });
      setStep("office");
    } catch (e) { setError((e as Error).message); }
  }

  async function showDebrief() {
    try {
      setDebrief(await api<Debrief>(`/invite/${token}/debrief`));
      setStep("debrief");
    } catch (e) { setError((e as Error).message); }
  }

  if (error) return <main className="page"><div className="error">{error}</div></main>;
  if (!info) return <main className="page muted">불러오는 중…</main>;
  if (info.state === "expired")
    return <main className="page"><div className="error">링크가 만료되었습니다. 채용 담당자에게 문의해 주세요.</div></main>;
  if (step === "office") return <Office token={token} onEnd={showDebrief} />;

  if (step === "debrief" && debrief)
    return (
      <main className="page">
        <h1>수고하셨습니다</h1>
        <div className="card">
          <h2>오늘 세션에서 연출된 상황</h2>
          <p className="muted">아래 상황은 협업 행동을 관찰하기 위해 미리 정해 둔 것입니다. 모든 지원자가 같은 시점에 같은 상황을 겪습니다.</p>
          <table><tbody>{debrief.events.map((e) => (
            <tr key={e.time}><td>{e.time}</td><td>{e.message}</td></tr>
          ))}</tbody></table>
        </div>
        <div className="card">{debrief.notice}</div>
      </main>
    );

  if (step === "ready")
    return (
      <main className="page" style={{ maxWidth: 640 }}>
        <h1>{info.company} · 루프랩 프로덕트 팀에 오신 걸 환영해요</h1>
        <div className="card">
          <p>오늘 당신은 리모트 스타트업 <b>루프랩</b>의 기획자로 합류합니다. 러닝 모임 앱 <b>모아런</b>을 만드는 팀이에요.</p>
          <ul>
            <li><b>한서윤 팀장</b>: 프로덕트 팀장. 팀 채널에서 과제를 공지합니다.</li>
            <li><b>이도현</b>: 입사 2년 차 기획자(AI 동료). 1:1 메신저로 함께 일합니다.</li>
            <li><b>AI 어시스턴트</b>: 질문·요약·초안 작성을 도와주는 업무 도구입니다.</li>
          </ul>
          <p className="muted">시작하면 {info.duration_minutes}분 타이머가 흐릅니다. 창을 닫으면 멈추고, 같은 링크로 다시 들어오면 이어집니다.</p>
          <button onClick={start}>시작하기</button>
        </div>
      </main>
    );

  return (
    <main className="page" style={{ maxWidth: 640 }}>
      <h1>{info.company} 협업 시뮬레이션 평가</h1>
      <p className="muted">{info.alias}님, 예상 소요 시간은 {info.duration_minutes}분입니다.</p>
      <div className="card">
        <h2>시작 전 안내</h2>
        <ul>
          <li><b>평가 형식</b>: 가상 회사에서 AI 동료와 함께 신규 기능 기획안을 작성합니다. 대화와 문서는 모두 텍스트로 진행됩니다.</li>
          <li><b>관찰 항목</b>: 책임감, 존중·소통, 협력 태도, 유연성, 그리고 AI 활용 방식.</li>
          <li><b>기록 방식</b>: 메시지, 응답 시점, 분업 결정, 문서 변경, AI 어시스턴트 요청과 결과 활용이 기록됩니다.</li>
          <li><b>AI 사용</b>: 동료와 팀장은 AI입니다. AI 어시스턴트는 자유롭게 쓸 수 있고, 사용 기록이 남습니다. 쓰지 않아도 불이익은 없습니다.</li>
          <li><b>최종 판단</b>: AI는 점수나 합격 여부를 정하지 않습니다. 관찰 기록을 채용 담당자가 직접 검토하고 판단합니다.</li>
        </ul>
        <label className="row" style={{ marginBottom: 12 }}>
          <input type="checkbox" style={{ width: "auto" }} checked={agree} onChange={(e) => setAgree(e.target.checked)} />
          위 내용을 이해했고 동의합니다.
        </label>
        <button disabled={!agree} onClick={consent}>동의하고 계속</button>
      </div>
    </main>
  );
}
