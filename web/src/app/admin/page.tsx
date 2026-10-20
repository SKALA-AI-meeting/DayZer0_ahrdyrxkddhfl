"use client";
// 지원자 목록(F-03)과 초대(F-02)
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { adminToken, api, ApiError, STATUS_LABEL } from "@/lib/api";

type Candidate = { id: string; alias: string; email: string; status: string; invite_path: string };

export default function AdminHome() {
  const router = useRouter();
  const [me, setMe] = useState<{ email: string; company: string } | null>(null);
  const [rows, setRows] = useState<Candidate[]>([]);
  const [alias, setAlias] = useState("");
  const [email, setEmail] = useState("");
  const [error, setError] = useState("");
  const [created, setCreated] = useState("");

  const load = useCallback(async () => {
    try {
      setMe(await api("/admin/me", {}, true));
      setRows(await api("/admin/candidates", {}, true));
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) router.push("/admin/login");
      else setError((err as Error).message);
    }
  }, [router]);

  useEffect(() => {
    load();
    const t = setInterval(load, 5000); // 상태(진행 중 → 리포트 준비됨) 변화를 보이게 주기적으로 새로고침
    return () => clearInterval(t);
  }, [load]);

  async function invite(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    try {
      const r = await api<{ invite_path: string }>("/admin/invites", {
        method: "POST", body: JSON.stringify({ alias, email }),
      }, true);
      setCreated(window.location.origin + r.invite_path);
      setAlias(""); setEmail("");
      load();
    } catch (err) {
      setError((err as Error).message);
    }
  }

  return (
    <main className="page">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h1>{me?.company ?? ""} 채용 · 지원자</h1>
        <button className="secondary" onClick={() => { adminToken.clear(); router.push("/admin/login"); }}>로그아웃</button>
      </div>

      <form className="card" onSubmit={invite}>
        <h2>지원자 초대</h2>
        <p className="muted">실명 대신 별칭을 씁니다. 링크는 1회용이며 72시간 뒤 만료됩니다.</p>
        <div className="row">
          <input placeholder="지원자 별칭 (예: 지원자-07)" value={alias} onChange={(e) => setAlias(e.target.value)} />
          <input placeholder="이메일 (선택)" value={email} onChange={(e) => setEmail(e.target.value)} />
          <button type="submit" style={{ whiteSpace: "nowrap" }}>초대 링크 만들기</button>
        </div>
        {created && (
          <p>초대 링크: <a href={created} target="_blank">{created}</a></p>
        )}
        {error && <div className="error">{error}</div>}
      </form>

      <div className="card">
        <table>
          <thead><tr><th>별칭</th><th>이메일</th><th>상태</th><th></th></tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id}>
                <td>{r.alias}</td>
                <td className="muted">{r.email}</td>
                <td><span className="badge">{STATUS_LABEL[r.status] ?? r.status}</span></td>
                <td>
                  {r.status === "report_ready" ? <Link href={`/admin/report/${r.id}`}>리포트 보기</Link>
                    : r.status === "invited" ? <a href={r.invite_path} target="_blank">링크</a> : null}
                </td>
              </tr>
            ))}
            {rows.length === 0 && <tr><td colSpan={4} className="muted">아직 초대한 지원자가 없습니다.</td></tr>}
          </tbody>
        </table>
      </div>
    </main>
  );
}
