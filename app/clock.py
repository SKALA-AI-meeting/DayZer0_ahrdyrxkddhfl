"""세션 시계.

벽시계가 아니라 "화면이 서버에 묻고 있던 시간"만 누적한다.
- 요청 사이 간격이 pause_after_idle_seconds 이하면 그 간격만큼 시간이 흐른다.
- 간격이 그보다 길면(창을 닫았다 돌아옴) 그 사이는 세지 않는다 → F-19 "남은 시간 그대로 이어짐".
- time_scale 배속을 곱해 "세션 안 시간(sim_seconds)"으로 저장한다. 배속을 중간에 바꿔도 시각이 튀지 않는다.
"""
from app.settings import SECONDS_PER_MINUTE


def advance(sim_seconds: float, last_seen: float | None, now: float,
            time_scale: float, pause_after_idle_seconds: float,
            duration_minutes: float) -> float:
    """요청 시각 now 기준으로 세션 안 시간을 갱신한다.

    Args:
        sim_seconds: 지금까지 흐른 세션 안 시간(초).
        last_seen: 직전 요청 시각(epoch 초). 첫 요청이면 None.
        now: 이번 요청 시각(epoch 초).
        time_scale: 배속.
        pause_after_idle_seconds: 이 간격을 넘으면 그 사이를 세지 않는다.
        duration_minutes: 세션 길이. 이를 넘지 않게 자른다.

    Returns:
        갱신된 세션 안 시간(초).
    """
    if last_seen is not None:
        gap = now - last_seen
        if 0 < gap <= pause_after_idle_seconds:
            sim_seconds += gap * time_scale
    return min(sim_seconds, duration_minutes * SECONDS_PER_MINUTE)


def minute_of(sim_seconds: float) -> float:
    """세션 안 시간(초)을 분으로 바꾼다."""
    return sim_seconds / SECONDS_PER_MINUTE


def due_scenes(all_scenes: tuple[dict, ...], elapsed_minute: float,
               already_run: set[str]) -> list[dict]:
    """지금 시각까지 실행됐어야 하는데 아직 안 된 장면을 시점 순으로 반환한다."""
    return [s for s in all_scenes
            if s["at_minute"] <= elapsed_minute and s["id"] not in already_run]


def available_docs(index: tuple[dict, ...], elapsed_minute: float) -> list[dict]:
    """세션 시각 기준으로 공개된 문서만 반환한다 (F-15)."""
    return [d for d in index if d["available_from_minute"] <= elapsed_minute]


def format_minute(minute: float) -> str:
    """세션 시각을 PRD 표기(HH:MM)로 바꾼다. 예: 65.0 → '01:05'."""
    total = int(minute)
    return f"{total // 60:02d}:{total % 60:02d}"
