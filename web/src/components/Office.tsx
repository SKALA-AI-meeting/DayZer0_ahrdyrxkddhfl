"use client";
// 가상 오피스(업무 보드 중심): 가운데 업무 카드 5장 · 업무 기록 · 회사 문서, 오른쪽 메신저(팀 채널 · 동료 1:1 · 팀장 1:1)
// 업무는 직접 하지 않는다: 카드에서 업무 번호를 고르면 정해진 지문이 이어지고 시간이 넘어간다(능력이 아니라 태도를 봄).
// 서버에 poll_interval_seconds마다 /poll을 불러 새 메시지와 상태를 받는다.
import { useCallback, useEffect, useRef, useState } from "react";
import { api, Msg } from "@/lib/api";

type Task = { part: string; title: string; owner: string; status: string };
type Poll = {
  minute: number; remaining: number; status: string; time: string; phase: string;
  messages: Msg[]; tasks: Task[]; docs: { id: string; title: string }[];
  progress: Record<string, { done: number; units: number; visible: boolean }>; can_finish: boolean;
  work_intro: string;
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

  // 업무는 글로 쓰지 않고 번호만 고른다(글솜씨가 평가에 섞이지 않게)
  async function work(part: string) {
    setWorking(true);
    try {
      await api(`/invite/${token}/work`, { method: "POST", body: JSON.stringify({ part }) });
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
      <header className="topbar">
        <span className="row"><span className="logo">Day <span>Zer0</span></span>
          <span className="muted">{state.pack.company}</span></span>
        <span className="row">남은 시간 <span className={`timer ${(shown ?? state.remaining) < 5 ? "low" : ""}`}>{mmss(shown ?? state.remaining)}</span></span>
        <button onClick={submit} disabled={!state.can_finish}
          title={state.can_finish ? "" : "팀장님이 보고를 요청한 뒤에 마칠 수 있어요"}>업무 마치기</button>
      </header>

      {/* 업무 보드: 업무 카드에서 번호를 골라 진행 · 업무 기록 · 회사 문서 */}
      <main className="workspace">
        <div className="ws-head">
          <h1>{state.pack.task_title}</h1>
          <p className="muted">{state.work_intro}</p>
        </div>

        <div className="board">
          {state.tasks.map((t) => {
            const p = state.progress[t.part];
            const done = !!p && p.visible && p.done >= p.units;
            return (
              <article key={t.part} className={`tcard ${t.owner}`}>
                <div className="tcard-top">
                  <span className="tnum">{t.part.padStart(2, "0")}</span>
                  <span className={`owner ${t.owner}`}>{N[t.owner]}</span>
                </div>
                <h3>{t.title}</h3>
                <div className="tcard-state">
                  {p && p.visible ? (
                    <span className="cells" title={`${p.done}/${p.units}칸`}>
                      {Array.from({ length: p.units }, (_, i) => <i key={i} className={i < p.done ? "on" : ""} />)}
                      <span className="muted small">{p.done}/{p.units}</span>
                    </span>
                  ) : <span className="muted small">{STATUS[t.status]}</span>}
                </div>
                <button className="tcard-btn" onClick={() => work(t.part)} disabled={working || done}>
                  {done ? "완료" : working ? "진행 중…" : "이 업무 하기"}
                </button>
              </article>
            );
          })}
        </div>

        <div className="ws-lower">
          <section className="sheet">
            <h2>업무 기록</h2>
            <div className="log">
              {workLog.length === 0 && <p className="muted small">아직 기록이 없어요. 위 업무 카드에서 &lsquo;이 업무 하기&rsquo;를 눌러 보세요.</p>}
              {workLog.map((m) => (
                <div key={m.id} className={`log-row ${m.sender === "candidate" ? "pick" : ""}`}>
                  <span className="time">{m.time}</span><span>{m.content}</span>
                </div>
              ))}
            </div>
          </section>
          <section className="sheet">
            <h2>회사 문서</h2>
            <p className="muted small">배경 자료예요. 업무 내용을 직접 쓸 필요는 없어요.</p>
            <div className="doc-list">
              {state.docs.map((d) => <button key={d.id} className="doc-row" onClick={() => openDoc(d.id)}>{d.title}</button>)}
            </div>
          </section>
        </div>
      </main>

      {/* 메신저 */}
      <aside className="messenger">
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
      </aside>

      {/* 회사 문서 보기: 가운데 창 */}
      {doc && (
        <div className="modal-bg" onClick={() => setDoc(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-head"><h2>{doc.title}</h2><button className="ghost" onClick={() => setDoc(null)}>닫기</button></div>
            <div className="modal-body"><DocBody text={doc.body} /></div>
          </div>
        </div>
      )}
    </div>
  );
}

function Bubble({ m, names, initials }: { m: Msg; names: Record<string, string>; initials: Record<string, string> }) {
  const mine = m.sender === "candidate";
  return (
    <div className={`bubble-row ${mine ? "mine" : ""}`}>
      <span className={`avatar ${m.sender}`}>{initials[m.sender] ?? "?"}</span>
      <div className="bubble"><span className="meta"><b>{mine ? "나" : names[m.sender]}</b><span className="ts">{m.time}</span></span>{m.content}</div>
    </div>
  );
}

// 회사 문서(마크다운)를 읽기 좋게 보여 준다. 쓰는 문법만(제목 · 목록 · 굵게 · 표) 다루고 라이브러리는 쓰지 않는다.
function inline(s: string) {
  return s.split(/(\*\*[^*]+\*\*)/).map((part, i) =>
    part.startsWith("**") && part.endsWith("**") ? <b key={i}>{part.slice(2, -2)}</b> : part);
}

function DocBody({ text }: { text: string }) {
  const out: React.ReactNode[] = [];
  const lines = text.split("\n");
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (line.startsWith("|")) {   // 표: 연속된 | 줄을 모은다(두 번째 줄은 구분선)
      const rows: string[][] = [];
      while (i < lines.length && lines[i].startsWith("|")) {
        if (!/^\|[\s|:-]+\|$/.test(lines[i])) rows.push(lines[i].split("|").slice(1, -1).map((c) => c.trim()));
        i++;
      }
      i--;
      out.push(<table key={i} className="doc-table"><thead><tr>{rows[0].map((c, j) => <th key={j}>{inline(c)}</th>)}</tr></thead>
        <tbody>{rows.slice(1).map((r, k) => <tr key={k}>{r.map((c, j) => <td key={j}>{inline(c)}</td>)}</tr>)}</tbody></table>);
    } else if (line.startsWith("# ")) out.push(<h3 key={i} className="doc-h1">{inline(line.slice(2))}</h3>);
    else if (line.startsWith("## ") || line.startsWith("### ")) out.push(<h4 key={i} className="doc-h2">{inline(line.replace(/^#+ /, ""))}</h4>);
    else if (/^(- |\d+\. )/.test(line)) out.push(<div key={i} className="doc-li">{inline(line)}</div>);
    else if (line.trim()) out.push(<p key={i}>{inline(line)}</p>);
  }
  return <>{out}</>;
}
