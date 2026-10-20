"use client";
// 리포트 보기(F-04) + 근거를 누르면 전체 대화의 해당 위치로 이동(F-05)
import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import Nav from "@/components/Nav";
import { api, Msg } from "@/lib/api";

type Observed = { behavior: string; quote: string; when: string; source?: string; message_id?: number | null; points?: string[] };
type Section = {
  element: string; situation: string; observed: Observed[]; note?: string; summary?: string;
  followups?: { id: number; when: string }[];
  review?: { when: string; quote: string; message_id: number | null }[];
  requests?: { when: string; request: string; reuse: string }[];
};
type Data = { alias: string; status: string; names: Record<string, string>; pack: { company: string; task_title: string }; report: { sections: Section[]; disclaimer: string } | null;
  report_state: string | null; messages: Msg[] };

export default function ReportPage() {
  const { sid } = useParams<{ sid: string }>();
  const [data, setData] = useState<Data | null>(null);
  const [error, setError] = useState("");
  const [focus, setFocus] = useState<number | null>(null);

  useEffect(() => {
    api<Data>(`/admin/reports/${sid}`, {}, true).then(setData).catch((e) => setError(e.message));
  }, [sid]);

  function jump(quote: string) {
    const hit = data?.messages.find((m) => m.sender === "candidate" && m.content.includes(quote.slice(0, 20)));
    if (!hit) return;
    jumpTo(hit.id);
  }

  function jumpTo(id: number) {
    setFocus(id);
    document.getElementById(`m${id}`)?.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  if (error) return <><Nav /><main className="page"><div className="error">{error}</div></main></>;
  if (!data) return <><Nav /><main className="page muted">불러오는 중…</main></>;

  return (
    <>
    <Nav />
    <main className="page">
      <Link href="/admin">← 지원자 목록</Link>
      <h1 style={{ marginTop: 8 }}>{data.alias} 관찰 리포트</h1>
      <p className="muted">시나리오: {data.pack.company} · {data.pack.task_title}</p>
      {!data.report ? (
        <div className="card muted">리포트 상태: {data.report_state ?? "생성 전"}</div>
      ) : (
        <>
          <p className="muted">{data.report.disclaimer}</p>
          {data.report.sections.map((s) => (
            <div className="card" key={s.element}>
              <h2>{s.element}</h2>
              <p className="muted">상황: {s.situation}</p>
              {s.summary && (
                <div className="notice small" style={{ marginBottom: 10 }}>
                  <b>근거 정리</b> <span className="muted">(AI가 아래 근거만 나란히 적은 것이며 판단이 아닙니다. 번호는 아래 근거 번호)</span>
                  <div style={{ marginTop: 4 }}>{s.summary}</div>
                </div>
              )}
              {s.observed.map((o, i) => (
                <div key={i} className="obs">
                  {o.points && o.points.length > 0 && (
                    <div className="muted small">[{i + 1}] 관찰 포인트: {o.points.join(" · ")}</div>
                  )}
                  <div>{!(o.points && o.points.length) && <span className="muted small">[{i + 1}] </span>}<span className="time">{o.source === "대화" ? `${o.when} 대화` : o.when}</span>{o.behavior}</div>
                  <div className="quote">
                    “{o.quote}”{" "}
                    {o.message_id ? <a href="#" onClick={(e) => { e.preventDefault(); jumpTo(o.message_id!); }}>대화에서 보기</a>
                      : !o.when.startsWith("기획안") && !o.source && <a href="#" onClick={(e) => { e.preventDefault(); jump(o.quote); }}>대화에서 보기</a>}
                  </div>
                </div>
              ))}
              {s.requests && s.requests.length > 0 && (
                <table>
                  <thead><tr><th>시각</th><th>어시스턴트 요청</th><th>기획안 반영</th></tr></thead>
                  <tbody>{s.requests.map((q, i) => (
                    <tr key={i}><td>{q.when}</td><td>{q.request}</td><td>{q.reuse}</td></tr>
                  ))}</tbody>
                </table>
              )}
              {s.review && s.review.length > 0 && (
                <div style={{ marginTop: 10 }}>
                  <div className="small"><b>판단이 필요한 원문</b> <span className="muted">(부정·제외 표현이 있어 AI가 자동 판정에서 뺐어요. 원문을 보고 판단해 주세요)</span></div>
                  {s.review.map((v, i) => (
                    <div key={i} className="quote">
                      <span className="time">{v.message_id ? `${v.when} 대화` : v.when}</span> “{v.quote}”{" "}
                      {v.message_id && <a href="#" onClick={(e) => { e.preventDefault(); jumpTo(v.message_id!); }}>대화에서 보기</a>}
                    </div>
                  ))}
                </div>
              )}
              {s.note && <p className="notice small">{s.note}</p>}
              {s.followups && (
                <p className="muted small">
                  {s.followups.length === 0 ? "이 상황 이후 지원자가 보낸 메시지 없음" : <>
                    이 상황 이후 지원자가 보낸 메시지 {s.followups.length}건 (판단 없이 위치만 표시):{" "}
                    {s.followups.map((f) => (
                      <a key={f.id} href="#" style={{ marginRight: 8 }}
                         onClick={(e) => { e.preventDefault(); jumpTo(f.id); }}>{f.when}</a>
                    ))}
                  </>}
                </p>
              )}
            </div>
          ))}
        </>
      )}
      <div className="card">
        <h2>전체 대화 기록</h2>
        {data.messages.map((m) => (
          <div key={m.id} id={`m${m.id}`} className={`msgline ${focus === m.id ? "highlight" : ""}`}>
            <span className="muted">[{m.time}][{m.channel}]</span>{" "}
            <span className="who">{m.sender === "candidate" ? data.alias : data.names[m.sender]}</span>{m.content}
          </div>
        ))}
      </div>
    </main>
    </>
  );
}
