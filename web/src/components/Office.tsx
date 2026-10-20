"use client";
// 가상 오피스: 메신저(팀 채널 · 동료 1:1 · 팀장 1:1) · 업무(업무 목록 · 업무 기록 · 작업 계속하기) · 회사 문서 · 남은 시간
// 업무는 직접 하지 않는다: 다음 몇 분 동안 할 일을 짧게 적으면 정해진 지문이 이어지고 시간이 넘어간다(능력이 아니라 태도를 봄).
// 서버에 poll_interval_seconds마다 /poll을 불러 새 메시지와 상태를 받는다.
import { useCallback, useEffect, useRef, useState } from "react";
import { api, Msg } from "@/lib/api";

type Task = { part: string; title: string; owner: string; status: string };
type Poll = {
  minute: number; remaining: number; status: string; time: string; phase: string;
  messages: Msg[]; tasks: Task[]; docs: { id: string; title: string }[];
  progress: Record<string, { done: number; units: number; visible: boolean }>; can_finish: boolean;
  work_intro: string; memo_max_chars: number;
  poll_interval_seconds: number; time_scale: number;
  activity: { actor: string; label: string; node: string }[];
  names: Record<string, string>;
  pack: { company: string; task_title: string; coworker_name: string; coworker_short: string; lead_name: string; lead_short: string };
};
type Tab = "team" | "dm" | "lead_dm";

// 탭 → 화면에 보일 채널. 시스템 알림은 팀 채널에 함께 보인다.
const TAB_CHANNELS: Record<Tab, string[]> = { team: ["team", "system"], dm: ["dm"], lead_dm: ["lead_dm"] };
// 탭 → 답하는 사람(입력 중 표시용). 팀 채널은 이름을 부르지 않으면 팀장이 답한다.
const TAB_ACTOR: Record<Tab, string> = { team: "lead", dm: "coworker", lead_dm: "lead" };

function mmss(minutes: number) {
  const s = Math.max(0, Math.round(minutes * 60));
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}

export default function Office({ token, onEnd }: { token: string; onEnd: () => void }) {
  const [state, setState] = useState<Poll | null>(null);
  const [messages, setMessages] = useState<Msg[]>([]);
  const [tab, setTab] = useState<Tab>("team");
  const [seen, setSeen] = useState<Record<Tab, number>>({ team: 0, dm: 0, lead_dm: 0 });  // 탭별 마지막으로 본 메시지 id
  const [draft, setDraft] = useState("");
  const [memo, setMemo] = useState("");
  const [busy, setBusy] = useState(false);
  const [working, setWorking] = useState(false);
  const [doc, setDoc] = useState<{ title: string; body: string } | null>(null);
  const [error, setError] = useState("");
  const lastId = useRef(0);
  const synced = useRef<{ remaining: number; at: number; scale: number } | null>(null);
  const [shown, setShown] = useState<number | null>(null);   // 화면에 보이는 남은 시간(분)

  const poll = useCallback(async () => {
    try {
      const p = await api<Poll>(`/invite/${token}/poll?after=${lastId.current}`);
      if (p.messages.length) {
        lastId.current = p.messages[p.messages.length - 1].id;
        // 요청이 겹쳐 같은 메시지를 두 번 받아도 id로 한 번만 보여준다
        setMessages((prev) => {
          const ids = new Set(prev.map((m) => m.id));
          return [...prev, ...p.messages.filter((m) => !ids.has(m.id))];
        });
      }
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

  // 지금 보고 있는 탭의 메시지는 읽은 것으로 표시한다
  useEffect(() => {
    const last = messages.filter((m) => TAB_CHANNELS[tab].includes(m.channel)).at(-1);
    if (last && last.id > seen[tab]) setSeen((s) => ({ ...s, [tab]: last.id }));
  }, [messages, tab, seen]);

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

  async function work() {
    if (!memo.trim()) return;
    setWorking(true);
    try {
      await api(`/invite/${token}/work`, { method: "POST", body: JSON.stringify({ text: memo }) });
      setMemo("");
      await poll();
    } catch (e) { setError((e as Error).message); }
    setWorking(false);
  }

  async function openDoc(id: string) {
    try { setDoc(await api(`/invite/${token}/docs/${id}`)); } catch (e) { setError((e as Error).message); }
  }

  async function submit() {
    if (!confirm("업무를 마치고 세션을 끝낼까요?")) return;
    try {
      await api(`/invite/${token}/submit`, { method: "POST" });
      onEnd();
    } catch (e) { setError((e as Error).message); }
  }

  if (!state) return <main className="page muted">오피스에 입장하는 중…</main>;
  const chat = messages.filter((m) => TAB_CHANNELS[tab].includes(m.channel));
  const workLog = messages.filter((m) => m.channel === "work");
  const unread = (t: Tab) => messages.filter((m) => TAB_CHANNELS[t].includes(m.channel)
    && m.sender !== "candidate" && m.id > seen[t]).length;
  // 이름은 세션의 시나리오 팩에서 온다(회사마다 등장인물이 다름)
  const N: Record<string, string> = { ...state.names, candidate: "나" };
  const INITIAL: Record<string, string> = { candidate: "나", coworker: state.pack.coworker_short, lead: state.pack.lead_short };
  const TAB_LABEL: Record<Tab, string> = { team: "# 팀 채널", dm: `${state.pack.coworker_name} 1:1`, lead_dm: `${state.pack.lead_name} 팀장 1:1` };
  const actor = TAB_ACTOR[tab];
  const STATUS: Record<string, string> = { todo: "할 일", doing: "진행 중", done: "완료" };

  return (
    <div className="office">
      <div className="topbar">
        <span className="row"><span className="logo">Day <span>Zer0</span></span>
          <span className="muted">{state.pack.company} · {state.pack.task_title}</span></span>
        <span className="row">남은 시간 <span className={`timer ${(shown ?? state.remaining) < 5 ? "low" : ""}`}>{mmss(shown ?? state.remaining)}</span></span>
        <button onClick={submit} disabled={!state.can_finish}
          title={state.can_finish ? "" : "팀장님이 보고를 요청한 뒤에 마칠 수 있어요"}>업무 마치기</button>
      </div>

      {/* 메신저 */}
      <section className="col">
        <div className="tabs">
          {(["team", "dm", "lead_dm"] as Tab[]).map((t) => (
            <button key={t} className={tab === t ? "active" : ""} onClick={() => setTab(t)}>
              {TAB_LABEL[t]}{tab !== t && unread(t) > 0 && <span className="unread">{unread(t)}</span>}
            </button>
          ))}
        </div>
        <div className="chat">
          {chat.length === 0 && <div className="typing">아직 메시지가 없어요.</div>}
          {chat.map((m) => m.sender === "system" ? (
            <div key={m.id} className="sysmsg">{m.time} · {m.content}</div>
          ) : (
            <Bubble key={m.id} m={m} names={N} initials={INITIAL} />
          ))}
          {/* 진행 이벤트: 서버가 알려 주는 AI의 현재 작업(Graph 노드) */}
          {state.activity.filter((a) => a.actor === actor).map((a) => (
            <div key={a.actor} className="typing">{N[a.actor]}님이 {a.label}…</div>
          ))}
          {busy && !state.activity.some((a) => a.actor === actor) && (
            <div className="typing">답장을 기다리는 중…</div>
          )}
        </div>
        <div className="composer">
          <textarea rows={2} value={draft} onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); send(); } }}
            placeholder={`${TAB_LABEL[tab]}에 메시지 (Enter 전송, Shift+Enter 줄바꿈)`} />
          <button onClick={send} disabled={busy}>보내기</button>
        </div>
        {error && <div className="error" onClick={() => setError("")}>{error}</div>}
      </section>

      {/* 업무: 업무 목록 · 업무 기록 · 작업 계속하기 */}
      <section className="col">
        <div className="notice small">{state.work_intro}</div>
        <div className="panel">
          <h2>업무 목록</h2>
          {state.tasks.map((t) => {
            const p = state.progress[t.part];
            return (
              <div className="task" key={t.part}>
                <span>{t.part}. {t.title} <span className={`owner ${t.owner}`}>{N[t.owner]}</span></span>
                {p && p.visible ? (
                  <span className="cells" title={`${p.done}/${p.units}칸`}>
                    {Array.from({ length: p.units }, (_, i) => <i key={i} className={i < p.done ? "on" : ""} />)}
                    <span className="muted small">{p.done}/{p.units}</span>
                  </span>
                ) : <span className="badge">{STATUS[t.status]}</span>}
              </div>
            );
          })}
        </div>
        <div className="panel work-panel">
          <h2 style={{ marginBottom: 0 }}>업무 기록</h2>
          <div className="chat">
            {workLog.length === 0 && <div className="typing">아직 업무 기록이 없어요. 아래에 다음에 할 일을 적어 보세요.</div>}
            {workLog.map((m) => m.sender === "candidate" ? (
              <div key={m.id} className="memo"><span className="time">{m.time}</span> 나의 메모: {m.content}</div>
            ) : (
              <div key={m.id} className="narration"><span className="time">{m.time}</span> {m.content}</div>
            ))}
          </div>
          <div className="composer">
            <textarea rows={2} value={memo} maxLength={state.memo_max_chars}
              onChange={(e) => setMemo(e.target.value)}
              placeholder={`다음 몇 분 동안 무엇을 할지 짧게 적어 주세요 (${state.memo_max_chars}자 이내)`} />
            <button onClick={work} disabled={working || !memo.trim()}>작업 계속하기</button>
          </div>
          <span className="muted small">{memo.length}/{state.memo_max_chars}자 · 누르면 적은 업무 하나가 한 칸 오르고 시간이 몇 분 넘어가요</span>
        </div>
      </section>

      {/* 회사 문서 */}
      <section className="col">
        <div className="panel">
          <h2>회사 문서</h2>
          <span className="muted small">배경 자료예요. 업무 내용을 직접 쓸 필요는 없어요.</span>
          <div className="doc-chips" style={{ marginTop: 8 }}>
            {state.docs.map((d) => <button key={d.id} className="chip" onClick={() => openDoc(d.id)}>{d.title}</button>)}
          </div>
          {doc && <div className="doc-body"><div className="row" style={{ justifyContent: "space-between" }}><b>{doc.title}</b>
            <button className="ghost" onClick={() => setDoc(null)}>닫기</button></div>{doc.body}</div>}
        </div>
      </section>
    </div>
  );
}

function Bubble({ m, names, initials }: { m: Msg; names: Record<string, string>; initials: Record<string, string> }) {
  const mine = m.sender === "candidate";
  return (
    <div className={`bubble-row ${mine ? "mine" : ""}`}>
      <span className={`avatar ${m.sender}`}>{initials[m.sender] ?? "?"}</span>
      <div className="bubble"><span className="meta">{mine ? "" : `${names[m.sender]} · `}{m.time}</span>{m.content}</div>
    </div>
  );
}
