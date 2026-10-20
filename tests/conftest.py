import pytest

from app import settings


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """테스트마다 빈 SQLite 파일을 쓴다. 검색은 기본 키워드(테스트에서 OpenAI를 부르지 않음)."""
    monkeypatch.setenv("DAYZERO_DB", str(tmp_path / "test.sqlite3"))
    monkeypatch.setitem(settings.config()["rag"], "search_mode", "keyword")
    yield
