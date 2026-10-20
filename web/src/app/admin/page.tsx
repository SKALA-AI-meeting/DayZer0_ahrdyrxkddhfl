"use client";
// 지원자 목록(F-03)과 초대(F-02). MVP는 이메일을 보내지 않으므로 링크를 복사해 전달한다.
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import DocAdmin from "@/components/DocAdmin";
import Nav from "@/components/Nav";
import { adminToken, api, ApiError, STATUS_LABEL } from "@/lib/api";

type Candidate = { id: string; alias: string; email: string; status: string; invite_path: string; scenario: string };
type Scenario = { id: string; company: string; service: string; task_title: string };

function CopyButton({ text }: { text: string }) {
  const [done, setDone] = useState(false);
  return (
    <button type="button" className="secondary" onClick={async () => {
      await navigator.clipboard.writeText(text);
      setDone(true);
      setTimeout(() => setDone(false), 1500);
    }}>{done ? "복사됨" : "링크 복사"}</button>
  );
}

export default function AdminHome() {
  const router = useRouter();
  const [me, setMe] = useState<{ email: string; company: string } | null>(null);
  const [rows, setRows] = useState<Candidate[]>([]);
  const [alias, setAlias] = useState("");
  const [email, setEmail] = useState("");
  const [scenarios, setScenarios] = useState<Scenario[]>([]);
  const [mine, setMine] = useState<{ current: Scenario; locked: boolean } | null>(null);
  const [error, setError] = useState("");
  const [created, setCreated] = useState<{ alias: string; url: string } | null>(null);
  const origin = typeof window === "undefined" ? "" : window.location.origin;

  const load = useCallback(async () => {
    try {
      setMe(await api("/admin/me", {}, true));
      setScenarios(await api("/admin/scenarios", {}, true));
      setMine(await api("/admin/scenario", {}, true));
      setRows(await api("/admin/candidates", {}, true));
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) router.push("/admin/login");
      else setError((err as Error).message);
    }
  }, [router]);

  useEffect(() => {
    load();
    const t = setInterval(load, 5000); // 진행 중 → 리포트 준비됨 같은 상태 변화를 보이게 주기적으로 새로고침
    return () => clearInterval(t);
  }, [load]);

  async function retry(id: string) {
    setError("");
    try {
      await api(`/admin/reports/${id}/retry`, { method: "POST" }, true);
      load();
    } catch (err) {
      setError((err as Error).message);
    }
  }

  async function chooseScenario(id: string) {
    setError("");
    try {
      setMine(await api("/admin/scenario", { method: "PUT", body: JSON.stringify({ scenario: id }) }, true));
    } catch (err) {
      setError((err as Error).message);
    }
  }

  async function invite(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    try {
      const r = await api<{ invite_path: string }>("/admin/invites", {
        method: "POST", body: JSON.stringify({ alias, email }),
      }, true);
      setCreated({ alias, url: origin + r.invite_path });
      setAlias(""); setEmail("");
      load();
    } catch (err) {
      setError((err as Error).message);
    }
  }

  return (
    <>
      <Nav right={<>
        <span className="muted small">{me?.company} · {me?.email}</span>
        <button className="ghost" onClick={() => { adminToken.clear(); router.push("/admin/login"); }}>로그아웃</button>
      </>} />
      <main className="page">
        <h1>지원자</h1>
        <p className="muted">지원자를 초대하고 진행 상태를 확인하세요. 세션이 끝나면 리포트가 자동으로 만들어집니다.</p>

        <form className="card" onSubmit={invite}>
          <h2>지원자 초대</h2>
          <p className="muted small">실명 대신 별칭을 씁니다. 링크는 지원자 한 명 전용이며 72시간 뒤 만료됩니다.</p>
          {mine && (
            <div className="notice small" style={{ marginBottom: 12 }}>
              업무 시나리오: <b>{mine.current.company} · {mine.current.task_title}</b>
              {mine.locked ? (
                <span className="muted"> — 이미 지원자를 초대해 바꿀 수 없어요. 모든 지원자가 같은 조건에서 평가받도록 한 기업은 한 시나리오만 씁니다.</span>
              ) : (
                <>
                  {" "}
                  <select value={mine.current.id} onChange={(e) => chooseScenario(e.target.value)} style={{ maxWidth: 300 }}>
                    {scenarios.map((s) => <option key={s.id} value={s.id}>{s.company} · {s.task_title}</option>)}
                  </select>
                  <span className="muted"> 첫 지원자를 초대하면 고정됩니다.</span>
                </>
              )}
            </div>
          )}
          <div className="row">
            <input placeholder="지원자 별칭 (예: 지원자-01)" value={alias} onChange={(e) => setAlias(e.target.value)} />
            <input placeholder="이메일 (기록용, 선택)" value={email} onChange={(e) => setEmail(e.target.value)} />
            <button type="submit">초대 링크 만들기</button>
          </div>
          {created && (
            <div className="notice" style={{ marginTop: 14 }}>
              <b>{created.alias}</b> 초대 링크가 만들어졌어요. 아래 링크를 지원자에게 전달해 주세요.
              <span className="small"> (MVP는 이메일을 자동으로 보내지 않습니다)</span>
              <div className="linkbox"><code>{created.url}</code><CopyButton text={created.url} /></div>
            </div>
          )}
          {error && <div className="error" style={{ marginTop: 10 }}>{error}</div>}
        </form>

        <div className="card">
          <table>
            <thead><tr><th>별칭</th><th>이메일</th><th>시나리오</th><th>상태</th><th style={{ textAlign: "right" }}>동작</th></tr></thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id}>
                  <td><b>{r.alias}</b></td>
                  <td className="muted">{r.email || "-"}</td>
                  <td className="muted small">{scenarios.find((s) => s.id === r.scenario)?.company ?? r.scenario}</td>
                  <td><span className={`badge ${r.status}`}>{STATUS_LABEL[r.status] ?? r.status}</span></td>
                  <td style={{ textAlign: "right" }}>
                    {r.status === "report_ready" ? <Link className="btn" href={`/admin/report/${r.id}`}>리포트 보기</Link>
                      : r.status === "failed" ? <button className="secondary" onClick={() => retry(r.id)}>리포트 다시 만들기</button>
                      : ["invited", "in_progress"].includes(r.status) ? <CopyButton text={origin + r.invite_path} />
                      : <span className="muted small">리포트 생성 중…</span>}
                  </td>
                </tr>
              ))}
              {rows.length === 0 && <tr><td colSpan={5} className="muted">아직 초대한 지원자가 없습니다. 위에서 초대 링크를 만들어 보세요.</td></tr>}
            </tbody>
          </table>
        </div>

        <DocAdmin key={mine?.current.id} />
      </main>
    </>
  );
}
