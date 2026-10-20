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
  part_titles: Record<string, string>; poll_interval_seconds: number; time_scale: number;
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
  const synced = useRef<{ remaining: number; at: number; scale: number } | null>(null);
  const [shown, setShown] = useState<number | null>(null);   // 화면에 보이는 남은 시간(분)
  const loadedParts = useRef(false);
  const saveTimers = useRef<Record<string, ReturnType<typeof setTimeout>>>({});

  const poll = useCallback(async () => {
    try {
      const p = await api<Poll>(`/invite/${token}/poll?after=${lastId.current}`);
      if (p.messages.length) {
        lastId.current = p.messages[p.messages.length - 1].id;
        // 요청이 겹쳐 같은 메시지를 두 번 받아도 id로 한 번만 보여준다
        setMessages((prev) => {
          const seen = new Set(prev.map((m) => m.id));
          return [...prev, ...p.messages.filter((m) => !seen.has(m.id))];
        });
      }
      if (!loadedParts.current) { setParts(p.my_parts); loadedParts.current = true; }  // 이어하기 시 복원
      setState(p);
      synced.current = { remaining: p.remaining, at: Date.now(), scale: p.time_scale };
      setShown(p.remaining);
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

  useEffect(() => {
    const t = setInterval(() => {
      const s = synced.current;
      if (!s) return;
      const elapsedMin = ((Date.now() - s.at) / 60000) * s.scale;
      setShown(Math.max(0, s.remaining - elapsedMin));
    }, 1000);
    return () => clearInterval(t);
  }, []);

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

  const pending = useRef<Record<string, string>>({});   // 아직 저장 안 된 입력

  function savePart(part: string, content: string) {
    delete pending.current[part];
    return api(`/invite/${token}/parts/${part}`, { method: "PUT", body: JSON.stringify({ content }) })
      .catch((e) => setError(e.message));
  }

  function editPart(part: string, content: string) {
    setParts((p) => ({ ...p, [part]: content }));
    pending.current[part] = content;
    clearTimeout(saveTimers.current[part]);
    saveTimers.current[part] = setTimeout(() => savePart(part, content), SAVE_DELAY_MS);
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
    // 자동 저장을 기다리지 않고, 남은 입력을 먼저 저장한 뒤 제출한다(마지막 수정 누락 방지)
    Object.values(saveTimers.current).forEach(clearTimeout);
    await Promise.all(Object.entries(pending.current).map(([part, content]) => savePart(part, content)));
    await api(`/invite/${token}/submit`, { method: "POST" });
    onEnd();
  }

  if (!state) return <main className="page muted">오피스에 입장하는 중…</main>;
  const chat = messages.filter((m) => (tab === "team" ? ["team", "system"] : ["dm"]).includes(m.channel));
  const assistantLog = messages.filter((m) => m.channel === "assistant");
  const STATUS: Record<string, string> = { todo: "할 일", doing: "진행 중", done: "완료" };

  return (
    <div className="office">
      <div className="topbar">
        <span className="row"><span className="logo">Day <span>Zer0</span></span>
          <span className="muted">루프랩 · 모아런 신규 기능 기획</span></span>
        <span className="row">남은 시간 <span className={`timer ${(shown ?? state.remaining) < 10 ? "low" : ""}`}>{mmss(shown ?? state.remaining)}</span></span>
        <button onClick={submit}>기획안 제출</button>
      </div>

      {/* 메신저 */}
      <section className="col">
        <div className="tabs">
          <button className={tab === "team" ? "active" : ""} onClick={() => setTab("team")}># 팀 채널</button>
          <button className={tab === "dm" ? "active" : ""} onClick={() => setTab("dm")}>이도현 · 1:1</button>
        </div>
        <div className="chat">
          {chat.length === 0 && <div className="typing">아직 메시지가 없어요.</div>}
          {chat.map((m) => m.sender === "system" ? (
            <div key={m.id} className="sysmsg">{m.time} · {m.content}</div>
          ) : (
            <Bubble key={m.id} m={m} />
          ))}
          {busy && <div className="typing">{tab === "dm" ? "이도현" : "한서윤 팀장"}님이 입력 중…</div>}
        </div>
        <div className="composer">
          <textarea rows={2} value={draft} onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); send(); } }}
            placeholder={tab === "team" ? "팀 채널에 메시지 (Enter 전송, Shift+Enter 줄바꿈)" : "이도현에게 메시지"} />
          <button onClick={send} disabled={busy}>보내기</button>
        </div>
        {error && <div className="error" onClick={() => setError("")}>{error}</div>}
      </section>

      {/* 공유 문서: 기획안 5파트 */}
      <section className="col">
        <div><h2 style={{ marginBottom: 2 }}>공유 문서 · 기획안</h2>
          <span className="muted small">입력하면 자동 저장됩니다. 편집은 나만 할 수 있고, 이도현·팀장은 읽을 수 있어요. 동료가 공유한 내용은 파란 상자로 보여요.</span></div>
        {Object.entries(state.part_titles).map(([part, title]) => {
          const owner = state.tasks.find((t) => t.part === part)?.owner ?? "candidate";
          return (
            <div className="part" key={part}>
              <h3>{part}. {title} <span className={`owner ${owner}`}>{NAME[owner]}</span></h3>
              <textarea rows={4} value={parts[part] ?? ""} onChange={(e) => editPart(part, e.target.value)}
                placeholder="여기에 작성하세요." />
              {state.coworker_parts[part] ? (
                <div className="shared"><b>이도현 공유</b>{"\n"}{state.coworker_parts[part]}</div>
              ) : owner !== "candidate" && (
                <div className="muted small" style={{ marginTop: 6 }}>
                  이도현 담당 · 작업 중이에요. 완성되면 여기에 공유돼요.
                </div>
              )}
            </div>
          );
        })}
      </section>

      {/* 보드 · 문서 · 어시스턴트 */}
      <section className="col">
        <div className="panel">
          <h2>태스크 보드</h2>
          {state.tasks.map((t) => (
            <div className="task" key={t.part}>
              <span>{t.part}. {t.title} <span className={`owner ${t.owner}`}>{NAME[t.owner]}</span></span>
              {["candidate", "both"].includes(t.owner) ? (
                <select value={t.status} onChange={(e) => setTask(t.part, e.target.value)} style={{ width: 96 }}>
                  <option value="todo">할 일</option><option value="doing">진행 중</option><option value="done">완료</option>
                </select>
              ) : <span className="badge">{STATUS[t.status]}</span>}
            </div>
          ))}
        </div>
        <div className="panel">
          <h2>회사 문서</h2>
          <div className="doc-chips">
            {state.docs.map((d) => <button key={d.id} className="chip" onClick={() => openDoc(d.id)}>{d.title}</button>)}
          </div>
          {doc && <div className="doc-body"><div className="row" style={{ justifyContent: "space-between" }}><b>{doc.title}</b>
            <button className="ghost" onClick={() => setDoc(null)}>닫기</button></div>{doc.body}</div>}
        </div>
        <div className="panel assistant-panel">
          <div><h2 style={{ marginBottom: 0 }}>AI 어시스턴트</h2>
            <span className="muted small">업무용 도구 · 회사 문서를 참고해 답해요</span></div>
          <div className="chat">
            {assistantLog.length === 0 && <div className="typing">질문, 요약, 초안 작성을 요청해 보세요.</div>}
            {assistantLog.map((m) => <Bubble key={m.id} m={m} />)}
            {asking && <div className="typing">답변 작성 중…</div>}
          </div>
          <div className="composer">
            <textarea rows={2} value={askDraft} onChange={(e) => setAskDraft(e.target.value)} placeholder="예: 지표 현황 요약해 줘" />
            <button onClick={ask} disabled={asking}>요청</button>
          </div>
        </div>
      </section>
    </div>
  );
}

const INITIAL: Record<string, string> = { candidate: "나", coworker: "도현", lead: "서윤", assistant: "AI" };

function Bubble({ m }: { m: Msg }) {
  const mine = m.sender === "candidate";
  return (
    <div className={`bubble-row ${mine ? "mine" : ""}`}>
      <span className={`avatar ${m.sender}`}>{INITIAL[m.sender] ?? "?"}</span>
      <div className="bubble"><span className="meta">{mine ? "" : `${NAME[m.sender]} · `}{m.time}</span>{m.content}</div>
    </div>
  );
}
