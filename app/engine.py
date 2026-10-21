"""세션 진행 엔진: 시계를 돌리고, 시각이 된 장면을 한 번씩 실행하고, 지원자 입력을 Graph로 넘긴다.

화면(또는 CLI)이 주기적으로 tick()을 부르면 그때 밀린 장면을 실행한다. 별도 스케줄러는 없다.

장면 실행 규칙(scenario/<팩>/events.toml):
- skip_if_candidate_wrote: 기준 장면 뒤 지원자가 그 채널에 이미 썼으면 실행하지 않는다(팀장이 먼저 묻기 전에 연락했는가).
- early_on_progress_check: 장면 시각 전이라도 지원자가 동료에게 진행 상황을 물으면 그때 실행한다.
- early_after_consult: 지원자가 팀장 1:1에 정해진 횟수만큼 쓰면 팀장이 답한 직후 실행한다(상한 시각은 at_minute).
업무는 직접 하지 않고 업무 메모 → 고정 지문 → 시계 넘기기로 진행한다(work_step, app/work.py).
"""
import asyncio
import logging
import time

from app import agent_client, clock, docs, guard, llm, observe, persona, settings, store, work
from app.mcp_client import call_business

log = logging.getLogger("dayzero.engine")
CANDIDATE_CHANNELS = ("team", "dm", "lead_dm")   # 지원자가 메시지를 보낼 수 있는 채널
SKIPPED_PREFIX = "skipped:"   # 조건 때문에 실행하지 않은 장면 표시(리포트의 규칙 관찰이 읽는다)


async def tick(session_id: str, now: float | None = None) -> dict:
    """세션 시계를 갱신하고, 지금까지 실행됐어야 할 장면을 순서대로 실행한다.

    Returns:
        현재 세션 시각(분), 남은 시간(분), 상태.
    """
    now = time.time() if now is None else now
    s = store.get_session(session_id)
    cfg = settings.config()["session"]
    if s["status"] != "in_progress":
        minute = clock.minute_of(s["sim_seconds"])
        return {"minute": minute, "remaining": max(0, cfg["duration_minutes"] - minute),
                "status": s["status"]}
    sim = clock.advance(s["sim_seconds"], s["last_seen"], now, cfg["time_scale"],
                        cfg["pause_after_idle_seconds"], cfg["duration_minutes"])
    store.update_session(session_id, sim_seconds=sim, last_seen=now)
    minute = clock.minute_of(sim)

    for scene in clock.due_scenes(settings.scenes(s["scenario"]), minute, store.run_scene_ids(session_id)):
        if not store.claim_scene(session_id, scene["id"], minute):
            continue   # 다른 요청이 이미 실행 중 (F-23 중복 방지)
        if should_skip(session_id, scene):
            store.claim_scene(session_id, f"{SKIPPED_PREFIX}{scene['id']}", minute)
            llm.trace({"session": session_id, "node": "engine.skip_scene", "scene": scene["id"]})
            continue
        try:
            await agent_client.run_turn(session_id, minute, {"kind": "scene", "scene_id": scene["id"]})
        except Exception:
            # 실패한 장면은 실행 표시를 지워 다음 tick에서 다시 시도한다(빠짐없이, F-23).
            store.unclaim_scene(session_id, scene["id"])
            log.exception("장면 실행 실패: session=%s scene=%s", session_id, scene["id"])
            break
        work.announce_effects(session_id, minute, s["scenario"])   # 예: 대상 변경 → 업무 진척 일부 무효 알림

    announce_shared_docs(session_id, minute)
    status = "in_progress"
    if minute >= cfg["duration_minutes"]:
        status = finish(session_id)
    return {"minute": minute, "remaining": max(0, cfg["duration_minutes"] - minute), "status": status}


def should_skip(session_id: str, scene: dict) -> bool:
    """skip_if_candidate_wrote 조건: 기준 장면 뒤 지원자가 지정 채널에 이미 썼으면 True.

    팀 채널 글은 팀장을 향한 말만 센다(동료 이름을 부른 말은 팀장에게 연락한 것이 아니다, persona.team_responder).
    """
    cond = scene.get("skip_if_candidate_wrote")
    if not cond:
        return False
    since = store.scene_run_minutes(session_id).get(cond["since_scene"])
    if since is None:
        return False
    pack = store.get_session(session_id)["scenario"]
    return any(m["sender"] == "candidate" and m["minute"] >= since
               and (m["channel"] != "team" or persona.team_responder(m["content"], pack) == "lead")
               for m in store.list_messages(session_id, channels=tuple(cond["channels"])))


def announce_shared_docs(session_id: str, minute: float) -> None:
    """늦게 공개되는 문서가 열리면 시스템 알림을 한 번 보낸다(F-15). 공개 시각은 문서 목록 데이터를 따른다."""
    s = store.get_session(session_id)
    for d in docs.all_docs(s["company"], s["scenario"]):
        # 장면 실행 기록을 같이 써서 여러 요청이 동시에 와도 한 번만 보낸다
        if 0 < d["available_from_minute"] <= minute and store.claim_scene(session_id, f"doc_shared:{d['id']}", minute):
            store.add_message(session_id, "system", "system",
                              settings.config()["session"]["doc_shared_notice"].format(title=d["title"]), minute)


async def send_message(session_id: str, channel: str, text: str, now: float | None = None) -> None:
    """지원자가 팀 채널(team), 동료 1:1(dm), 팀장 1:1(lead_dm)에 메시지를 보낸다."""
    if channel not in CANDIDATE_CHANNELS:
        raise ValueError(f"보낼 수 없는 채널: {channel}")
    state = await tick(session_id, now)
    if state["status"] != "in_progress":
        raise RuntimeError("이미 끝난 세션입니다.")
    minute = state["minute"]
    pack = store.get_session(session_id)["scenario"]
    store.add_message(session_id, channel, "candidate", text, minute)
    if settings.config()["session"]["closing_scene"] in store.run_scene_ids(session_id):
        return   # 팀장이 마무리한 뒤에는 기록만 한다(대화는 끝났다)
    role = persona.responder(channel, text, pack)
    if guard.is_evaluation_question(text):
        store.add_message(session_id, channel, role, guard.notice(), minute)
        return
    if channel == "dm" and await start_split_early(session_id, text, minute):
        return
    if role == "coworker" and await reveal_early(session_id, text, minute):
        return
    try:
        await agent_client.run_turn(session_id, minute, {"kind": "candidate", "channel": channel, "text": text})
    except Exception:   # 재시도 후에도 실패: 500 대신 정해진 문장으로 답하고 원인은 로그에 남긴다(PRD A.4)
        log.exception("답장 생성 실패: session=%s channel=%s", session_id, channel)
        store.add_message(session_id, channel, role,
                          settings.config()["verify"]["llm_failure_reply"], minute)
    if channel == "lead_dm":
        await run_after_consult(session_id, minute)


async def _run_scene_now(session_id: str, scene_id: str, minute: float) -> bool:
    """장면을 지금 실행한다. 이미 실행됐거나 실패하면 False(실패하면 원래 시각에 다시 실행되게 표시를 지운다)."""
    if not store.claim_scene(session_id, scene_id, minute):
        return False
    try:
        await agent_client.run_turn(session_id, minute, {"kind": "scene", "scene_id": scene_id})
    except Exception:
        store.unclaim_scene(session_id, scene_id)
        log.exception("장면 앞당겨 실행 실패: session=%s scene=%s", session_id, scene_id)
        return False
    work.announce_effects(session_id, minute, store.get_session(session_id)["scenario"])
    return True


async def start_split_early(session_id: str, text: str, minute: float) -> bool:
    """분업 장면 전에 지원자가 먼저 분담을 꺼내면 분업 장면(F-21)을 지금 실행한다.

    분업 장면은 흐름용 시점이라 앞당겨도 내용(동료가 분담안 제안 → 수락·조정)은 같다.
    장면 실행 기록을 남기므로 정해진 시각에 같은 분담안을 다시 말하지 않고, 이후 답은 분업 단계로 처리된다.

    Returns:
        분업 장면을 실행했으면 True.
    """
    s = store.get_session(session_id)
    phrases = settings.config()["negotiation"]["split_request_phrases"]
    if s["phase"] != "waiting" or not any(p in text for p in phrases):
        return False
    return await _run_scene_now(session_id, observe.SPLIT_SCENE, minute)


async def reveal_early(session_id: str, text: str, minute: float) -> bool:
    """early_on_progress_check 장면 전에 지원자가 동료에게 진행 상황을 물으면 그 장면을 지금 실행한다.

    중간 점검을 한 지원자는 동료의 지연을 일찍 알게 된다. 사건 내용은 같고 발견 시점만 지원자 행동에 따라 달라진다.

    Returns:
        장면을 실행했으면 True(그 장면 대사가 이번 메시지에 대한 답이 된다).
    """
    s = store.get_session(session_id)
    if not observe.is_progress_question(text):
        return False
    # 분업 협상 중이어도 털어놓는다. 이벤트 장면이라 Graph가 먼저 지금 분담안으로 확정한다(finalize_split).
    if s["phase"] not in ("negotiate", "work"):
        return False
    ran = store.run_scene_ids(session_id)
    for scene in settings.scenes(s["scenario"]):
        if scene.get("early_on_progress_check") and scene["id"] not in ran:
            if await _run_scene_now(session_id, scene["id"], minute):
                llm.trace({"session": session_id, "node": "engine.reveal_early", "scene": scene["id"]})
                return True
    return False


async def run_after_consult(session_id: str, minute: float) -> None:
    """early_after_consult 장면: 기준 장면 뒤 지원자가 팀장 1:1에 정해진 횟수만큼 썼으면 지금 실행한다."""
    s = store.get_session(session_id)
    runs = store.scene_run_minutes(session_id)
    turns = settings.config()["work"]["consult_turns_before_pivot"]
    for scene in settings.scenes(s["scenario"]):
        cond = scene.get("early_after_consult")
        if not cond or scene["id"] in runs or cond["since_scene"] not in runs:
            continue
        since = runs[cond["since_scene"]]
        sent = [m for m in store.list_messages(session_id, channels=("lead_dm",))
                if m["sender"] == "candidate" and m["minute"] >= since]
        if len(sent) >= turns:
            await _run_scene_now(session_id, scene["id"], minute)


def skip_target(session_id: str, minute: float, pack: str | None = None) -> float:
    """'이 업무 하기'로 넘어갈 세션 시각(분). skip_minutes만큼 넘기되 다음 장면 시각과 세션 끝을 넘지 않는다."""
    cfg = settings.config()
    ran = store.run_scene_ids(session_id)
    upcoming = [sc["at_minute"] for sc in settings.scenes(pack) if sc["id"] not in ran and sc["at_minute"] > minute]
    return min([minute + cfg["work"]["skip_minutes"], cfg["session"]["duration_minutes"]] + upcoming[:1])


async def work_step(session_id: str, part: str, now: float | None = None) -> dict:
    """지원자가 고른 업무(1~5번) 하나를 한 칸 진행하고, 고정 지문을 남긴 뒤 시계를 넘긴다.

    업무를 글로 쓰게 하면 글솜씨·구체성(능력)이 섞이므로 번호만 고르게 한다. LLM은 쓰지 않는다.
    내가 맡은 업무면 '내 업무', 동료가 맡은 업무면 '동료 업무 메우기'로 코드가 정한다(work.resolve).

    Raises:
        ValueError: 없는 업무 번호.
        RuntimeError: 이미 끝난 세션.
    """
    now = time.time() if now is None else now
    if part not in settings.PART_IDS:
        raise ValueError(f"업무 번호는 {', '.join(settings.PART_IDS)} 중 하나여야 해요.")
    state = await tick(session_id, now)
    if state["status"] != "in_progress":
        raise RuntimeError("이미 끝난 세션입니다.")
    minute, pack = state["minute"], store.get_session(session_id)["scenario"]
    mid = store.add_message(session_id, "work", "candidate", f"{work.part_name(part, pack)} 진행", minute)
    category, part = work.resolve(session_id, "own", part, pack)
    advanced = work.can_advance(session_id, part, pack)   # 다 끝났거나 장면 영향으로 막히면 오르지 않음
    store.add_work_memo(session_id, mid, category, part, advanced)
    if part:   # 업무 카드 상태도 진척에 맞춘다(business-mcp 변경 도구)
        done, need = work.progress(session_id, pack)[part], work.units(pack)[part]
        await call_business("update_task_status", {"session_id": session_id, "part": part,
                                                   "status": work.task_status(done, need)})
    text = work.narration(session_id, category, part, advanced, pack)
    store.add_message(session_id, "work", "narrator", f"[{work.label(category, pack)}] {text}", minute)
    target = skip_target(session_id, minute, pack)
    store.update_session(session_id, sim_seconds=target * settings.SECONDS_PER_MINUTE, last_seen=now)
    llm.trace({"session": session_id, "node": "engine.work_step", "category": category, "part": part,
               "advanced": advanced, "from": round(minute, 2), "to": round(target, 2)})
    return await tick(session_id, now)


def can_finish(session_id: str) -> bool:
    """지원자가 '업무 마치기'를 누를 수 있는가: 보고 요청 장면(config [session].finish_after_scene)이 실행된 뒤."""
    return settings.config()["session"]["finish_after_scene"] in store.run_scene_ids(session_id)


_report_tasks: set[asyncio.Task] = set()   # 백그라운드 리포트 작업 참조 보관(가비지 수집 방지)


def finish(session_id: str) -> str:
    """세션을 끝내고(제출 또는 시간 종료, F-17) 리포트 생성을 백그라운드로 시작한다(F-32).

    실행 중인 이벤트 루프 안에서 불러야 한다.
    """
    if store.get_session(session_id)["status"] == "in_progress":
        store.update_session(session_id, status="completed", phase="done")
        schedule_report(session_id)
    return "completed"


def schedule_report(session_id: str) -> None:
    """리포트 생성을 백그라운드 작업으로 시작한다(실행 중인 이벤트 루프 안에서 부른다)."""
    task = asyncio.get_running_loop().create_task(agent_client.generate_report(session_id))
    _report_tasks.add(task)
    task.add_done_callback(_report_tasks.discard)


async def wait_reports() -> None:
    """진행 중인 리포트 작업이 끝날 때까지 기다린다(CLI·테스트용)."""
    if _report_tasks:
        await asyncio.gather(*_report_tasks, return_exceptions=True)
