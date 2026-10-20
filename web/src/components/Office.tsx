"use client";
// 가상 오피스: 메신저(F-12) · 공유 문서(F-13) · 태스크 보드(F-14) · 회사 문서(F-15)
//            · AI 어시스턴트(F-16) · 남은 시간·제출(F-17)
// 서버에 poll_interval_seconds마다 /poll을 불러 새 메시지와 상태를 받는다.
import { useCallback, useEffect, useRef, useState } from "react";
import { api, Msg, NAME } from "@/lib/api";

type Task = { part: string; title: string; owner: string; status: string };
type Poll = {
  minute: number; remaining: number; status: string; time: string; phase: string;
  messages: Msg[]; tasks: Task[]; my_parts: Record<string, string>;
  coworker_parts: Record<string, string>; docs: { id: string; title: string }[];
  part_titles: Record<string, string>; poll_interval_seconds: number;
};

const SAVE_DELAY_MS = 1000; // 입력이 멈춘 뒤 자동 저장까지 기다리는 시간(UI 반응성용)

function mmss(minutes: number) {
  const s = Math.max(0, Math.round(minutes * 60));
  return `${String(Math.floor(s / 3600)).padStart(1, "0")}:${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}

export default function Office({ token, onEnd }: { token: string; onEnd: () => void }) {
  const [state, setState] = useState<Poll | null>(null);
  const [messages, setMessages] = useState<Msg[]>([]);
  const [tab, setTab] = useState<"team" | "dm">("team");
  const [draft, setDraft] = useState("");
  const [askDraft, setAskDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [asking, setAsking] = useState(false);
  const [parts, setParts] = useState<Record<string, string>>({});
  const [doc, setDoc] = useState<{ title: string; body: string } | null>(null);
  const [error, setError] = useState("");
  const lastId = useRef(0);
  const loadedParts = useRef(false);
  const saveTimers = useRef<Record<string, ReturnType<typeof setTimeout>>>({});

  const poll = useCallback(async () => {
    try {
      const p = await api<Poll>(`/invite/${token}/poll?after=${lastId.current}`);
      if (p.messages.length) {
        lastId.current = p.messages[p.messages.length - 1].id;
        setMessages((prev) => [...prev, ...p.messages]);
      }
      if (!loadedParts.current) { setParts(p.my_parts); loadedParts.current = true; }  // 이어하기 시 복원
      setState(p);
      if (p.status !== "in_progress") onEnd();
    } catch (e) {
      setError((e as Error).message);
    }
  }, [token, onEnd]);

  useEffect(() => {
    poll();
    const ms = (state?.poll_interval_seconds ?? 2) * 1000;
    const t = setInterval(poll, ms);
    return () => clearInterval(t);
  }, [poll, state?.poll_interval_seconds]);

  async function send() {
    if (!draft.trim()) return;
    setBusy(true);
    try {
      await api(`/invite/${token}/messages`, { method: "POST", body: JSON.stringify({ channel: tab, text: draft }) });
      setDraft("");
      await poll();
    } catch (e) { setError((e as Error).message); }
    setBusy(false);
  }

  async function ask() {
    if (!askDraft.trim()) return;
    setAsking(true);
    try {
      await api(`/invite/${token}/assistant`, { method: "POST", body: JSON.stringify({ text: askDraft }) });
      setAskDraft("");
      await poll();
    } catch (e) { setError((e as Error).message); }
    setAsking(false);
  }

  function editPart(part: string, content: string) {
    setParts((p) => ({ ...p, [part]: content }));
    clearTimeout(saveTimers.current[part]);
    saveTimers.current[part] = setTimeout(() => {
      api(`/invite/${token}/parts/${part}`, { method: "PUT", body: JSON.stringify({ content }) })
        .catch((e) => setError(e.message));
    }, SAVE_DELAY_MS);
  }

  async function setTask(part: string, status: string) {
    try {
      await api(`/invite/${token}/tasks/${part}`, { method: "PUT", body: JSON.stringify({ status }) });
      await poll();
    } catch (e) { setError((e as Error).message); }
  }

  async function openDoc(id: string) {
    try { setDoc(await api(`/invite/${token}/docs/${id}`)); } catch (e) { setError((e as Error).message); }
  }

  async function submit() {
    if (!confirm("기획안을 제출하고 세션을 끝낼까요?")) return;
    await api(`/invite/${token}/submit`, { method: "POST" });
    onEnd();
  }

  if (!state) return <main className="page muted">오피스에 입장하는 중…</main>;
  const chat = messages.filter((m) => (tab === "team" ? ["team", "system"] : ["dm"]).includes(m.channel));
  const assistantLog = messages.filter((m) => m.channel === "assistant");

  return (
    <div className="office">
      <div className="topbar">
        <b>루프랩 · 모아런 신규 기능 기획</b>
        <span>남은 시간 <span className="timer">{mmss(state.remaining)}</span></span>
        <button onClick={submit}>기획안 제출</button>
      </div>

      {/* 메신저 */}
      <section className="col">
        <div className="tabs">
          <button className={tab === "team" ? "" : "secondary"} onClick={() => setTab("team")}># 팀 채널</button>
          <button className={tab === "dm" ? "" : "secondary"} onClick={() => setTab("dm")}>이도현 (1:1)</button>
        </div>
        <div className="chat">
          {chat.map((m) => (
            <div key={m.id} className={`msg ${m.sender === "candidate" ? "mine" : ""} ${m.sender === "system" ? "system" : ""}`}>
              <span className="muted">{m.time}</span> <span className="who">{NAME[m.sender]}</span>{m.content}
            </div>
          ))}
          {busy && <div className="muted">답장을 기다리는 중…</div>}
        </div>
        <div className="row">
          <textarea rows={2} value={draft} onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); send(); } }}
            placeholder={tab === "team" ? "팀 채널에 메시지" : "이도현에게 메시지"} />
          <button onClick={send} disabled={busy}>보내기</button>
        </div>
        {error && <div className="error" onClick={() => setError("")}>{error}</div>}
      </section>

      {/* 공유 문서: 기획안 5파트 */}
      <section className="col">
        <h2>공유 문서 · 기획안</h2>
        {Object.entries(state.part_titles).map(([part, title]) => (
          <div className="part" key={part}>
            <h3>{part}. {title}</h3>
            <textarea rows={4} value={parts[part] ?? ""} onChange={(e) => editPart(part, e.target.value)}
              placeholder="여기에 작성하면 자동 저장됩니다." />
            {state.coworker_parts[part] && (
              <div className="shared"><b>이도현 공유</b>{"\n"}{state.coworker_parts[part]}</div>
            )}
          </div>
        ))}
      </section>

      {/* 보드 · 문서 · 어시스턴트 */}
      <section className="col">
        <div>
          <h2>태스크 보드</h2>
          {state.tasks.map((t) => (
            <div className="task" key={t.part}>
              <span>{t.part}. {t.title}<br /><span className="muted">{NAME[t.owner]}</span></span>
              {["candidate", "both"].includes(t.owner) ? (
                <select value={t.status} onChange={(e) => setTask(t.part, e.target.value)} style={{ width: 90 }}>
                  <option value="todo">할 일</option><option value="doing">진행 중</option><option value="done">완료</option>
                </select>
              ) : <span className="badge">{{ todo: "할 일", doing: "진행 중", done: "완료" }[t.status]}</span>}
            </div>
          ))}
        </div>
        <div>
          <h2>회사 문서</h2>
          {state.docs.map((d) => <div key={d.id}><a href="#" onClick={(e) => { e.preventDefault(); openDoc(d.id); }}>{d.title}</a></div>)}
          {doc && <div className="doc-body"><b>{doc.title}</b> <a href="#" onClick={(e) => { e.preventDefault(); setDoc(null); }}>닫기</a>{"\n\n"}{doc.body}</div>}
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 8, flex: 1, minHeight: 240 }}>
          <h2>AI 어시스턴트</h2>
          <div className="chat">
            {assistantLog.map((m) => (
              <div key={m.id} className={`msg ${m.sender === "candidate" ? "mine" : ""}`}>
                <span className="who">{NAME[m.sender]}</span>{m.content}
              </div>
            ))}
            {asking && <div className="muted">답변 작성 중…</div>}
          </div>
          <div className="row">
            <textarea rows={2} value={askDraft} onChange={(e) => setAskDraft(e.target.value)} placeholder="질문·요약·초안 요청" />
            <button onClick={ask} disabled={asking}>요청</button>
          </div>
        </div>
      </section>
    </div>
  );
}
