"""config.toml, scenario/, prompts/ 를 읽는 모듈.

사람이 고른 숫자는 config.toml, 시나리오 내용은 scenario/, 페르소나 지시문은 prompts/ 에 있다.
코드에는 숫자·문장을 직접 쓰지 않고 여기서 읽어 쓴다.
"""
import os
import tomllib
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
SECONDS_PER_MINUTE = 60
# 모든 모듈이 이 파일을 먼저 불러오므로 여기서 .env를 읽는다.
# override=True: 셸에 같은 이름의 변수(예: 다른 OPENAI_API_KEY)가 있어도 이 프로젝트 값을 쓴다.
load_dotenv(ROOT / ".env", override=True)


@lru_cache
def config() -> dict:
    """config.toml 전체를 dict로 반환한다."""
    return tomllib.loads((ROOT / "config.toml").read_text(encoding="utf-8"))


@lru_cache
def scenes() -> tuple[dict, ...]:
    """세션 장면(킥오프·이벤트·마감)을 시점 순서로 반환한다."""
    data = tomllib.loads((ROOT / "scenario" / "events.toml").read_text(encoding="utf-8"))
    return tuple(sorted(data["scene"], key=lambda s: s["at_minute"]))


def scene_by_id(scene_id: str) -> dict:
    """장면 id로 장면 정의를 찾는다.

    Raises:
        KeyError: 없는 장면 id.
    """
    for scene in scenes():
        if scene["id"] == scene_id:
            return scene
    raise KeyError(f"정의되지 않은 장면: {scene_id}")


@lru_cache
def docs_index() -> tuple[dict, ...]:
    """회사 문서 목록(id, title, file, available_from_minute)을 반환한다."""
    data = tomllib.loads((ROOT / "scenario" / "docs" / "index.toml").read_text(encoding="utf-8"))
    return tuple(data["doc"])


@lru_cache
def doc_text(doc_id: str) -> str:
    """회사 문서 본문을 반환한다.

    Raises:
        KeyError: 없는 문서 id.
    """
    for doc in docs_index():
        if doc["id"] == doc_id:
            return (ROOT / "scenario" / "docs" / doc["file"]).read_text(encoding="utf-8")
    raise KeyError(f"없는 문서: {doc_id}")


@lru_cache
def observation_spec() -> dict:
    """관찰 항목 정의(scenario/observation.toml)를 반환한다."""
    return tomllib.loads((ROOT / "scenario" / "observation.toml").read_text(encoding="utf-8"))


@lru_cache
def coworker_parts() -> tuple[dict, ...]:
    """01:30에 동료가 공유하는 자기 파트 내용을 반환한다."""
    data = tomllib.loads((ROOT / "scenario" / "coworker_parts.toml").read_text(encoding="utf-8"))
    return tuple(data["part"])


@lru_cache
def prompt(name: str) -> str:
    """prompts/{name}.md 를 읽는다."""
    return (ROOT / "prompts" / f"{name}.md").read_text(encoding="utf-8").strip()


@lru_cache
def default_assignment() -> dict[str, str]:
    """PRD 6.2 기본 분담안. 파트 번호(문자열) → 담당자."""
    return {"1": "candidate", "2": "candidate", "3": "both", "4": "coworker", "5": "coworker"}


PART_TITLES = {
    "1": "문제 정의와 대상 사용자",
    "2": "사용자 인터뷰에서 찾은 근거",
    "3": "핵심 기능 제안",
    "4": "비슷한 서비스 사례",
    "5": "성공 지표와 출시 일정",
}


def db_path() -> Path:
    """SQLite 파일 위치. 테스트에서는 DAYZERO_DB 환경변수로 바꾼다."""
    return Path(os.environ.get("DAYZERO_DB", ROOT / "data" / "dayzero.sqlite3"))
