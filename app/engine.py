"""세션 진행 엔진: 시계를 돌리고, 시각이 된 장면을 한 번씩 실행하고, 지원자 입력을 Graph로 넘긴다.

화면(또는 CLI)이 주기적으로 tick()을 부르면 그때 밀린 장면을 실행한다. 별도 스케줄러는 없다.
"""
import asyncio
import logging
import time

from app import assistant, clock, graph, guard, report, settings, store

log = logging.getLogger("dayzero.engine")


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

    for scene in clock.due_scenes(settings.scenes(), minute, store.run_scene_ids(session_id)):
        if not store.claim_scene(session_id, scene["id"], minute):
            continue   # 다른 요청이 이미 실행 중 (F-23 중복 방지)
        try:
            await graph.run_turn(session_id, minute, {"kind": "scene", "scene_id": scene["id"]})
        except Exception:
            # 실패한 장면은 실행 표시를 지워 다음 tick에서 다시 시도한다(빠짐없이, F-23).
            store.unclaim_scene(session_id, scene["id"])
            log.exception("장면 실행 실패: session=%s scene=%s", session_id, scene["id"])
            break

    status = "in_progress"
    if minute >= cfg["duration_minutes"]:
        status = finish(session_id)
    return {"minute": minute, "remaining": max(0, cfg["duration_minutes"] - minute), "status": status}


async def send_message(session_id: str, channel: str, text: str, now: float | None = None) -> None:
    """지원자가 팀 채널(team) 또는 동료 1:1(dm)에 메시지를 보낸다."""
    if channel not in ("team", "dm"):
        raise ValueError(f"보낼 수 없는 채널: {channel}")
    state = await tick(session_id, now)
    if state["status"] != "in_progress":
        raise RuntimeError("이미 끝난 세션입니다.")
    minute = state["minute"]
    store.add_message(session_id, channel, "candidate", text, minute)
    if guard.is_evaluation_question(text):
        role = "coworker" if channel == "dm" else "lead"
        store.add_message(session_id, channel, role, guard.notice(), minute)
        return
    try:
        await graph.run_turn(session_id, minute, {"kind": "candidate", "channel": channel, "text": text})
    except Exception:   # 재시도 후에도 실패: 500 대신 정해진 문장으로 답하고 원인은 로그에 남긴다(PRD A.4)
        log.exception("답장 생성 실패: session=%s channel=%s", session_id, channel)
        role = "coworker" if channel == "dm" else "lead"
        store.add_message(session_id, channel, role,
                          settings.config()["verify"]["llm_failure_reply"], minute)


async def ask_assistant(session_id: str, text: str, now: float | None = None) -> str:
    """지원자가 AI 어시스턴트에 요청한다."""
    state = await tick(session_id, now)
    if state["status"] != "in_progress":
        raise RuntimeError("이미 끝난 세션입니다.")
    return await assistant.ask(session_id, state["minute"], text)


def save_part(session_id: str, part: str, content: str) -> None:
    """지원자가 기획안 파트를 저장한다(자동 저장, F-13). 동료 파트 키(coworker:*)는 쓸 수 없다."""
    if part not in settings.PART_TITLES:
        raise ValueError(f"없는 파트: {part}")
    s = store.get_session(session_id)
    store.upsert_part(session_id, part, content, clock.minute_of(s["sim_seconds"]))


_report_tasks: set[asyncio.Task] = set()   # 백그라운드 리포트 작업 참조 보관(가비지 수집 방지)


def finish(session_id: str) -> str:
    """세션을 끝내고(제출 또는 시간 종료, F-17) 리포트 생성을 백그라운드로 시작한다(F-32).

    실행 중인 이벤트 루프 안에서 불러야 한다.
    """
    if store.get_session(session_id)["status"] == "in_progress":
        store.update_session(session_id, status="completed", phase="done")
        task = asyncio.get_running_loop().create_task(report.generate_with_retry(session_id))
        _report_tasks.add(task)
        task.add_done_callback(_report_tasks.discard)
    return "completed"


async def wait_reports() -> None:
    """진행 중인 리포트 작업이 끝날 때까지 기다린다(CLI·테스트용)."""
    if _report_tasks:
        await asyncio.gather(*_report_tasks, return_exceptions=True)
