import pytest

from app import settings


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """테스트마다 빈 SQLite 파일을 쓴다. 검색은 기본 키워드(테스트에서 OpenAI를 부르지 않음)."""
    monkeypatch.setenv("DAYZERO_DB", str(tmp_path / "test.sqlite3"))
    monkeypatch.setenv("LANGSMITH_TRACING", "false")   # 테스트(가짜 LLM)는 LangSmith로 trace를 보내지 않는다
    monkeypatch.setitem(settings.config()["rag"], "search_mode", "keyword")
    yield


@pytest.fixture
def late_interviews(monkeypatch):
    """문서 공개 시각 장치(F-15) 검사용: 인터뷰 요약을 01:05(65분)에 공개되는 문서로 바꾼다.

    지금 시나리오는 업무를 지문으로 처리해 모든 문서를 처음부터 공개하지만, 늦게 공개하는 장치(검색·열람 차단,
    공개 알림, 발화 검증)는 다른 시나리오가 쓸 수 있게 남아 있다.
    """
    patched = tuple({**d, "available_from_minute": 65} if d["id"] == "interviews" else d
                    for d in settings.docs_index())
    monkeypatch.setattr(settings, "docs_index", lambda pack=None: patched)
