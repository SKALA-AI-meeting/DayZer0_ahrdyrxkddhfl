"use client";
// 회사 문서 관리(F-06): 등록 요청 → (멈춤) 미리보기 확인 → 승인/거절 → 승인된 문서만 AI가 참고
import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";

type Doc = { id: string; title: string; source: string };
type Req = { request_id: string; doc_id: string; title: string; body: string; masked_count: number; status: string };
const REQ_LABEL: Record<string, string> = { waiting: "승인 대기", registered: "등록됨", rejected: "거절됨" };

export default function DocAdmin() {
  const [docs, setDocs] = useState<Doc[]>([]);
  const [reqs, setReqs] = useState<Req[]>([]);
  const [form, setForm] = useState({ doc_id: "", title: "", body: "" });
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      const r = await api<{ docs: Doc[]; requests: Req[] }>("/admin/docs", {}, true);
      setDocs(r.docs); setReqs(r.requests);
    } catch (e) { setError((e as Error).message); }
  }, []);
  useEffect(() => { load(); }, [load]);

  async function submit(e: React.FormEvent) {
    e.preventDefault(); setError("");
    try {
      await api("/admin/docs", { method: "POST", body: JSON.stringify(form) }, true);
      setForm({ doc_id: "", title: "", body: "" });
      load();
    } catch (err) { setError((err as Error).message); }
  }

  async function decide(id: string, approved: boolean) {
    try {
      await api(`/admin/docs/${id}/decision`, { method: "POST", body: JSON.stringify({ approved }) }, true);
      load();
    } catch (err) { setError((err as Error).message); }
  }

  return (
    <div className="card">
      <h2>회사 문서 관리</h2>
      <p className="muted">문서를 추가하거나 같은 문서 id로 교체할 수 있습니다. 승인한 뒤에만 AI 동료와 AI 어시스턴트가 참고합니다. 이메일·전화번호는 자동으로 가립니다.</p>
      <p className="muted">현재 문서: {docs.map((d) => `${d.title}(${d.id}${d.source === "company" ? ", 등록" : ""})`).join(" · ")}</p>
      <form onSubmit={submit} style={{ display: "grid", gap: 6 }}>
        <div className="row">
          <input placeholder="문서 id (예: design-guide, 교체는 policy 등 기존 id)" value={form.doc_id}
            onChange={(e) => setForm({ ...form, doc_id: e.target.value })} />
          <input placeholder="제목" value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} />
        </div>
        <textarea rows={4} placeholder="본문" value={form.body} onChange={(e) => setForm({ ...form, body: e.target.value })} />
        <button type="submit">등록 요청</button>
      </form>
      {reqs.length > 0 && (
        <table style={{ marginTop: 12 }}>
          <thead><tr><th>요청</th><th>미리보기 (마스킹 후)</th><th>상태</th></tr></thead>
          <tbody>{reqs.map((r) => (
            <tr key={r.request_id}>
              <td>{r.title}<br /><span className="muted">{r.doc_id}</span></td>
              <td style={{ whiteSpace: "pre-wrap" }}>{r.body.slice(0, 300)}{r.masked_count > 0 && <div className="muted">개인정보 {r.masked_count}건 가림</div>}</td>
              <td>
                {r.status === "waiting" ? (
                  <div className="row">
                    <button onClick={() => decide(r.request_id, true)}>승인</button>
                    <button className="secondary" onClick={() => decide(r.request_id, false)}>거절</button>
                  </div>
                ) : <span className={`badge ${r.status}`}>{REQ_LABEL[r.status] ?? r.status}</span>}
              </td>
            </tr>
          ))}</tbody>
        </table>
      )}
      {error && <div className="error" style={{ marginTop: 8 }}>{error}</div>}
    </div>
  );
}
