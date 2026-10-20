"use client";
// 리포트 보기(F-04) + 근거를 누르면 전체 대화의 해당 위치로 이동(F-05)
import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import { api, Msg, NAME } from "@/lib/api";

type Observed = { behavior: string; quote: string; when: string };
type Section = {
  element: string; situation: string; observed: Observed[]; note?: string;
  requests?: { when: string; request: string; reuse: string }[];
};
type Data = { alias: string; status: string; report: { sections: Section[]; disclaimer: string } | null;
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
    setFocus(hit.id);
    document.getElementById(`m${hit.id}`)?.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  if (error) return <main className="page"><div className="error">{error}</div></main>;
  if (!data) return <main className="page muted">불러오는 중…</main>;

  return (
    <main className="page">
      <Link href="/admin">← 지원자 목록</Link>
      <h1 style={{ marginTop: 8 }}>{data.alias} 관찰 리포트</h1>
      {!data.report ? (
        <div className="card muted">리포트 상태: {data.report_state ?? "생성 전"}</div>
      ) : (
        <>
          <p className="muted">{data.report.disclaimer}</p>
          {data.report.sections.map((s) => (
            <div className="card" key={s.element}>
              <h2>{s.element}</h2>
              <p className="muted">상황: {s.situation}</p>
              {s.observed.map((o, i) => (
                <div key={i} style={{ marginBottom: 10 }}>
                  <div><b>{o.when}</b> {o.behavior}</div>
                  <div className="quote">
                    “{o.quote}” {o.when !== "기획안" && <a href="#" onClick={(e) => { e.preventDefault(); jump(o.quote); }}>대화에서 보기</a>}
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
              {s.note && <p className="muted">{s.note}</p>}
            </div>
          ))}
        </>
      )}
      <div className="card">
        <h2>전체 대화 기록</h2>
        {data.messages.map((m) => (
          <div key={m.id} id={`m${m.id}`} className={`msg ${focus === m.id ? "highlight" : ""}`}>
            <span className="muted">[{m.time}][{m.channel}]</span>{" "}
            <span className="who">{m.sender === "candidate" ? data.alias : NAME[m.sender]}</span>{m.content}
          </div>
        ))}
      </div>
    </main>
  );
}
