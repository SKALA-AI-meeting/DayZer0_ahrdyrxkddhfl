"""config.toml, scenario/, prompts/ 를 읽는 모듈.

사람이 고른 숫자는 config.toml, 시나리오 내용은 scenario/<팩>/, 페르소나 지시문은 prompts/ 에 있다.
코드에는 숫자·문장을 직접 쓰지 않고 여기서 읽어 쓴다.

시나리오 팩: 기업(채용 건)마다 하나를 고른다(scenario/packs.toml). 팩마다 회사·과제·문서·대사는 다르고
장면 구조·관찰 요소는 같아서, 에이전트 코드는 그대로 두고 데이터만 갈아끼운다.
"""
import os
import random
import tomllib
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
SECONDS_PER_MINUTE = 60
PART_IDS = ("1", "2", "3", "4", "5")   # 업무(태스크 카드) 번호(모든 팩 공통)
# 모든 모듈이 이 파일을 먼저 불러오므로 여기서 .env를 읽는다.
# override=True: 셸에 같은 이름의 변수(예: 다른 OPENAI_API_KEY)가 있어도 이 프로젝트 값을 쓴다.
load_dotenv(ROOT / ".env", override=True)


@lru_cache
def config() -> dict:
    """config.toml 전체를 dict로 반환한다."""
    return tomllib.loads((ROOT / "config.toml").read_text(encoding="utf-8"))


# ---------- 시나리오 팩 ----------

@lru_cache
def _packs_file() -> dict:
    return tomllib.loads((ROOT / "scenario" / "packs.toml").read_text(encoding="utf-8"))


def default_pack() -> str:
    return _packs_file()["default"]


def pack_ids() -> list[str]:
    return [p["id"] for p in _packs_file()["pack"]]


RANDOM_PACK = "random"   # 기업이 고를 수 있는 특수 값: 지원자(초대)마다 팩을 무작위로 고른다


def pick_pack(choice: str) -> str:
    """기업이 정한 시나리오로 이번 지원자의 팩을 정한다. RANDOM_PACK이면 등록된 팩 중 하나를 무작위로 고른다.

    고른 실제 팩 id가 세션에 저장되므로 그 뒤 화면·관찰·리포트는 모두 그 팩을 따른다.
    """
    return random.choice(pack_ids()) if choice == RANDOM_PACK else choice


def _dir(pack: str | None) -> Path:
    pack = pack or default_pack()
    for p in _packs_file()["pack"]:
        if p["id"] == pack:
            return ROOT / "scenario" / p["dir"]
    raise KeyError(f"없는 시나리오 팩: {pack}")


@lru_cache
def pack_info(pack: str | None = None) -> dict:
    """팩의 회사·서비스·등장인물·파트 제목(scenario/<팩>/pack.toml)."""
    data = tomllib.loads((_dir(pack) / "pack.toml").read_text(encoding="utf-8"))
    return {"id": pack or default_pack(), **data}


def part_titles(pack: str | None = None) -> dict[str, str]:
    return dict(pack_info(pack)["part_titles"])


def names(pack: str | None = None) -> dict[str, str]:
    """화면·대화 기록용 이름. 역할 키(coworker, lead)는 모든 팩 공통. narrator는 업무 지문."""
    info = pack_info(pack)
    return {"candidate": "지원자", "coworker": info["coworker_name"], "lead": f"{info['lead_name']} 팀장",
            "system": "시스템", "narrator": "업무 진행", "both": "함께"}


@lru_cache
def scenes(pack: str | None = None) -> tuple[dict, ...]:
    """세션 장면(킥오프·이벤트·마감)을 시점 순서로 반환한다."""
    data = tomllib.loads((_dir(pack) / "events.toml").read_text(encoding="utf-8"))
    return tuple(sorted(data["scene"], key=lambda s: s["at_minute"]))


def scene_by_id(scene_id: str, pack: str | None = None) -> dict:
    """장면 id로 장면 정의를 찾는다.

    Raises:
        KeyError: 없는 장면 id.
    """
    for scene in scenes(pack):
        if scene["id"] == scene_id:
            return scene
    raise KeyError(f"정의되지 않은 장면: {scene_id}")


@lru_cache
def docs_index(pack: str | None = None) -> tuple[dict, ...]:
    """회사 문서 목록(id, title, file, available_from_minute)을 반환한다."""
    data = tomllib.loads((_dir(pack) / "docs" / "index.toml").read_text(encoding="utf-8"))
    return tuple(data["doc"])


@lru_cache
def doc_text(doc_id: str, pack: str | None = None) -> str:
    """회사 문서 본문을 반환한다.

    Raises:
        KeyError: 없는 문서 id.
    """
    for doc in docs_index(pack):
        if doc["id"] == doc_id:
            return (_dir(pack) / "docs" / doc["file"]).read_text(encoding="utf-8")
    raise KeyError(f"없는 문서: {doc_id}")


@lru_cache
def observation_spec(pack: str | None = None) -> dict:
    """관찰 항목 정의(scenario/<팩>/observation.toml)를 반환한다."""
    return tomllib.loads((_dir(pack) / "observation.toml").read_text(encoding="utf-8"))


@lru_cache
def work_spec(pack: str | None = None) -> dict:
    """업무 메모 범주와 범주·단계별 지문(scenario/<팩>/work.toml)."""
    return tomllib.loads((_dir(pack) / "work.toml").read_text(encoding="utf-8"))


def brief(pack: str | None = None) -> str:
    return (_dir(pack) / "brief.md").read_text(encoding="utf-8")


# ---------- 프롬프트 ----------

@lru_cache
def _prompt_raw(name: str) -> str:
    return (ROOT / "prompts" / f"{name}.md").read_text(encoding="utf-8").strip()


def prompt(name: str, pack: str | None = None) -> str:
    """prompts/{name}.md 를 읽고 {company}, {coworker_name} 같은 자리에 팩 값을 넣는다."""
    text = _prompt_raw(name)
    for key, value in pack_info(pack).items():
        if isinstance(value, str):
            text = text.replace("{" + key + "}", value)
    return text


def default_assignment(pack: str | None = None) -> dict[str, str]:
    """동료가 처음 제안하는 분담안(pack.toml). 업무 번호(문자열) → candidate | coworker | both."""
    return dict(pack_info(pack)["default_assignment"])


def db_path() -> Path:
    """SQLite 파일 위치. 테스트에서는 DAYZERO_DB 환경변수로 바꾼다."""
    return Path(os.environ.get("DAYZERO_DB", ROOT / "data" / "dayzero.sqlite3"))
