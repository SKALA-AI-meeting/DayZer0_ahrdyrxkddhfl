"use client";
// 지원자 흐름: 링크 입장(F-10) → 안내·동의(F-11) → 시작 대기 → 가상 오피스 → 디브리핑(F-18)
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import Nav from "@/components/Nav";
import Office from "@/components/Office";
import { api } from "@/lib/api";

type Pack = { company: string; company_desc: string; service: string; service_desc: string;
  lead_name: string; lead_short: string; lead_role: string; coworker_name: string; coworker_short: string; coworker_role: string };
type Info = { state: string; company: string; alias: string; duration_minutes: number;
  consented: boolean; started: boolean; pack: Pack };
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

  const STEP_NAMES = ["안내·동의", "준비", "업무", "디브리핑"];
  const stepIndex = { intro: 0, ready: 1, office: 2, debrief: 3 }[step];
  const Shell = ({ children }: { children: React.ReactNode }) => (
    <>
      <Nav right={info ? <span className="muted small">{info.company} · {info.alias}</span> : null} />
      <main className="page narrow" style={{ maxWidth: 680 }}>
        <div className="steps">{STEP_NAMES.map((n, i) => (
          <div key={n} className={i === stepIndex ? "on" : i < stepIndex ? "done" : ""}>{i + 1}. {n}</div>
        ))}</div>
        {children}
      </main>
    </>
  );

  if (error) return <Shell><div className="error">{error}</div></Shell>;
  if (!info) return <Shell><div className="card muted">불러오는 중…</div></Shell>;
  if (info.state === "expired")
    return <Shell><div className="error">링크가 만료되었습니다. 채용 담당자에게 문의해 주세요.</div></Shell>;
  if (step === "office") return <Office token={token} onEnd={showDebrief} />;

  if (step === "debrief" && debrief)
    return (
      <Shell>
        <div className="card">
          <h1>수고하셨습니다</h1>
          <p className="muted">세션이 끝났어요. 오늘 있었던 일을 설명해 드릴게요.</p>
          <h2 style={{ marginTop: 16 }}>연출된 상황 4가지</h2>
          <p className="muted small">협업 행동을 관찰하기 위해 미리 정해 둔 상황이에요. 모든 지원자가 같은 시점에 같은 상황을 겪습니다.</p>
          <table><tbody>{debrief.events.map((e) => (
            <tr key={e.time}><td style={{ width: 70 }}><span className="time">{e.time}</span></td><td>{e.message}</td></tr>
          ))}</tbody></table>
        </div>
        <div className="notice">{debrief.notice}</div>
      </Shell>
    );

  if (step === "ready")
    return (
      <Shell>
        <div className="card">
          <h1>{info.pack.company} 팀에 오신 걸 환영해요</h1>
          <p>오늘 당신은 <b>{info.pack.company}</b>({info.pack.company_desc})의 기획자로 합류합니다. <b>{info.pack.service}</b>({info.pack.service_desc})를 만드는 팀이에요.</p>
          <div className="stack" style={{ margin: "14px 0" }}>
            <div className="row"><span className="avatar lead">{info.pack.lead_short}</span><div><b>{info.pack.lead_name} 팀장</b><div className="muted small">{info.pack.lead_role}. 팀 채널에서 과제를 공지해요.</div></div></div>
            <div className="row"><span className="avatar coworker">{info.pack.coworker_short}</span><div><b>{info.pack.coworker_name}</b><div className="muted small">{info.pack.coworker_role}(AI 동료). 1:1 메신저로 함께 일해요.</div></div></div>
            <div className="row"><span className="avatar assistant">AI</span><div><b>AI 어시스턴트</b><div className="muted small">질문·요약·초안을 도와주는 업무 도구예요.</div></div></div>
          </div>
          <p className="notice small">시작하면 {info.duration_minutes}분 타이머가 흐릅니다. 창을 닫으면 멈추고, 같은 링크로 다시 들어오면 이어집니다.</p>
          <button onClick={start} style={{ width: "100%", marginTop: 8 }}>시작하기</button>
        </div>
      </Shell>
    );

  return (
    <Shell>
      <div className="card">
        <h1>{info.company} 협업 시뮬레이션 평가</h1>
        <p className="muted">{info.alias}님, 예상 소요 시간은 <b>{info.duration_minutes}분</b>입니다. 가입 없이 이 링크로 진행해요.</p>
        <h2 style={{ marginTop: 16 }}>시작 전 안내</h2>
        <ul>
          <li><b>평가 형식</b>: 가상 회사에서 AI 동료와 함께 신규 기능 기획안을 작성합니다. 대화와 문서는 모두 텍스트로 진행됩니다.</li>
          <li><b>관찰 항목</b>: 책임감, 존중·소통, 협력 태도, 유연성, 그리고 AI 활용 방식.</li>
          <li><b>기록 방식</b>: 메시지, 응답 시점, 분업 결정, 문서 변경, AI 어시스턴트 요청과 결과 활용이 기록됩니다.</li>
          <li><b>AI 사용</b>: 동료와 팀장은 AI입니다. AI 어시스턴트는 자유롭게 쓸 수 있고, 사용 기록이 남습니다. 쓰지 않아도 불이익은 없습니다.</li>
          <li><b>최종 판단</b>: AI는 점수나 합격 여부를 정하지 않습니다. 관찰 기록을 채용 담당자가 직접 검토하고 판단합니다.</li>
        </ul>
        <label className="row" style={{ margin: "12px 0" }}>
          <input type="checkbox" style={{ width: "auto" }} checked={agree} onChange={(e) => setAgree(e.target.checked)} />
          위 내용을 이해했고 동의합니다.
        </label>
        <button disabled={!agree} onClick={consent} style={{ width: "100%" }}>동의하고 계속</button>
      </div>
    </Shell>
  );
}
