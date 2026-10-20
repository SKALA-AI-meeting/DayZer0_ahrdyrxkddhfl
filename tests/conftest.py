import pytest


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """테스트마다 빈 SQLite 파일을 쓴다."""
    monkeypatch.setenv("DAYZERO_DB", str(tmp_path / "test.sqlite3"))
    yield
