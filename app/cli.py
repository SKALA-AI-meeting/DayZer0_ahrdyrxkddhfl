"""터미널 시뮬레이션: 가짜 시계로 40분 세션을 몇 분 안에 돌린다.

실행: uv run python -m app.cli [대본 경로] [시나리오 팩]
- 실제 시간을 기다리지 않고 now 값을 STEP_SECONDS씩 올려 tick을 부른다.
- 대본(eval/scripts/*.toml)의 지원자 행동을 정해진 분에 실행한다.
"""
import asyncio
import sys
import tomllib
from pathlib import Path

from app import clock, engine, persona, settings, store

STEP_SECONDS = 10   # 가짜 시계 한 칸(초). pause_after_idle_seconds보다 작아야 시계가 흐른다.


def _print_new(session_id: str, after_id: int) -> int:
    for m in store.list_messages(session_id, after_id=after_id):
        print(f"[{clock.format_minute(m['minute'])}][{m['channel']:9}] "
              f"{settings.names(store.get_session(session_id)['scenario'])[m['sender']]}: {m['content']}")
        after_id = m["id"]
    return after_id


async def main(script_path: Path, scenario: str | None = None) -> None:
    actions = tomllib.loads(script_path.read_text(encoding="utf-8"))["action"]
    scale = settings.config()["session"]["time_scale"]
    sid = store.create_session("데모지원자", scenario=scenario)
    print(f"세션 {sid} 시작 (대본: {script_path.name})")
    now, last_id, done = 1_000_000.0, 0, set()
    while True:
        state = await engine.tick(sid, now)
        for i, act in enumerate(actions):
            if i in done or act["at_minute"] > state["minute"]:
                continue
            done.add(i)
            if act["kind"] in engine.CANDIDATE_CHANNELS:
                await engine.send_message(sid, act["kind"], act["text"], now)
            elif act["kind"] == "work":
                await engine.work_step(sid, act["text"], now)
            elif act["kind"] == "submit":
                engine.finish(sid)
        last_id = _print_new(sid, last_id)
        if store.get_session(sid)["status"] != "in_progress":
            break
        now += STEP_SECONDS / scale * scale   # 실제 간격은 STEP_SECONDS (배속은 tick이 적용)
    print("\n리포트 생성 중...")
    await engine.wait_reports()
    rep = store.get_report(sid)
    print(f"\n== 리포트 (상태: {rep['state'] if rep else '없음'}) ==")
    for sec in (rep or {}).get("body", {}).get("sections", []) if rep and rep["body"] else []:
        print(f"\n[{sec['element']}] 등급: {sec.get('grade', '-')}  상황: {sec['situation']}")
        for o in sec["observed"]:
            print(f"  - {o['when']} {o['behavior']}\n      근거: \"{o['quote']}\"")
        if sec.get("note"):
            print(f"  ({sec['note']})")
    for memo in (rep or {}).get("body", {}).get("memos", []) if rep and rep["body"] else []:
        print(f"  · {memo['when']} 업무 메모 [{memo['category']}{' / ' + memo['part'] if memo.get('part') else ''}"
              f"{' +1칸' if memo.get('advanced') else ''}] {memo['memo']}")
    if rep and rep["body"]:
        print("업무 진척:", ", ".join(f"{p['part']} {p['done']}/{p['units']}" for p in rep["body"].get("progress", [])))
    s = store.get_session(sid)
    print("\n== 결과 ==")
    print("상태:", s["status"], "/ 분업:", persona.assignment_text(s["assignment"], s["scenario"]),
          "/ 조정 횟수:", s["negotiation_round"])
    print("실행된 장면:", sorted(store.run_scene_ids(sid)))
    print("태스크 보드:", [(t["part"], t["owner"]) for t in store.list_tasks(sid)])
    print("세션 id:", sid)


if __name__ == "__main__":
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else settings.ROOT / "eval" / "scripts" / "demo.toml"
    pack = sys.argv[2] if len(sys.argv) > 2 else None   # 시나리오 팩(기본: packs.toml default)
    asyncio.run(main(path, pack))
