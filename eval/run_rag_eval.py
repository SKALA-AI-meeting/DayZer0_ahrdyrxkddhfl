"""검색 평가: 같은 질문으로 keyword vs vector(실제 임베딩)를 비교한다.

실행: uv run python eval/run_rag_eval.py
- 질문은 문서 단어를 그대로 쓰지 않고 바꿔 말한 것 위주(실제 지원자처럼).
- hit@1: 정답 문서가 1위, hit@3: 상위 3개 안. 결과를 eval/results.md 뒤에 덧붙인다.
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["DAYZERO_DB"] = str(Path(tempfile.mkdtemp()) / "rag.sqlite3")

from app import settings  # noqa: E402
from mcp_servers import rag  # noqa: E402

CASES = [   # (질문, 정답 문서)
    ("올해 돈 받는 기능 내도 돼?", "policy"),
    ("사용자 위치를 얼마나 자세히 보여줄 수 있어?", "policy"),
    ("작은 모임일수록 다시 오는 비율이 높아?", "metrics"),
    ("첫 모임 뒤 일주일 안에 앱을 안 연 사람이 얼마나 돼?", "metrics"),
    ("사람들이 왜 두 번째 모임에 안 나왔대?", "interviews"),
    ("모임장이 따로 연락해 주면 다시 오나?", "interviews"),
    ("이번 분기에 제일 중요한 목표가 뭐였지?", "meeting"),
    ("지난 회의에 누가 참석했어?", "meeting"),
    ("우리 앱 핵심 기능이 뭐야?", "company"),
    ("주로 어떤 사람들이 앱을 써?", "company"),
]
MINUTE = 70   # 인터뷰 요약까지 모두 공개된 시각


def run(mode: str) -> tuple[int, int, list[str]]:
    settings.config()["rag"]["search_mode"] = mode
    h1 = h3 = 0
    misses = []
    for q, answer in CASES:
        hits = [h for h in rag.search_company_docs(q, MINUTE) if "excerpt" in h]
        ids = [h["doc_id"] for h in hits]
        if hits and hits[0].get("via") != mode:
            misses.append(f"{q} (→ {hits[0].get('via')}로 대체됨)")
        h1 += bool(ids) and ids[0] == answer
        h3 += answer in ids[:3]
        if not ids or ids[0] != answer:
            misses.append(f"{q} → {ids[:3]} (정답 {answer})")
    return h1, h3, misses


def main() -> str:
    rows, notes = [], []
    for mode in ("keyword", "vector"):
        h1, h3, misses = run(mode)
        n = len(CASES)
        rows.append(f"| {mode} | {h1}/{n} | {h3}/{n} |")
        notes += [f"- [{mode}] {m}" for m in misses]
    report = "\n".join(["", "## 검색 평가 (keyword vs vector, 바꿔 말한 질문 10개)", "",
                        "| 방식 | 정답 문서 1위 (hit@1) | 상위 3개 안 (hit@3) |", "|---|---|---|",
                        *rows, "", "놓친 질문:", *(notes or ["- 없음"]), ""])
    with (ROOT / "eval" / "results.md").open("a", encoding="utf-8") as f:
        f.write(report)
    return report


if __name__ == "__main__":
    print(main())
