"""관찰 판별 평가: B(하이브리드) 반복 일관성·정확도, A(규칙만)와 비교.

실행: uv run python eval/run_eval.py [반복 횟수=3]
- 평가셋 eval/cases.toml 의 사례마다 임시 DB에 대화를 넣고 판별한다(동료 대사는 고정, LLM은 판별에만).
- B는 캐시 없이 N번 돌린다. 일관성 = N번 결과가 모두 같은 체크 비율.
- 정확도 = 정답 라벨과 같은 비율. 요소 단위(관련 행동이 있었나)로 A와 B를 나란히 비교한다.
- 결과는 eval/results.md 에 저장한다(Harness "바꾸기 전/후 결과표" 증빙).
"""
import asyncio
import os
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["DAYZERO_DB"] = str(Path(tempfile.mkdtemp()) / "eval.sqlite3")

from app import observe, settings, store  # noqa: E402  (DB 경로를 먼저 정해야 함)

SENDER = {"team": "candidate", "dm": "candidate", "assistant": "candidate", "assistant_reply": "assistant"}


def load_case(case: dict) -> str:
    """사례 하나를 새 세션으로 DB에 넣고 세션 id를 반환한다."""
    sid = store.create_session(case["name"])
    events = []
    for scene in settings.scenes():
        store.claim_scene(sid, scene["id"], scene["at_minute"])
        if scene["speaker"] != "system" or scene["element"]:
            channel = "system" if scene["speaker"] == "system" else scene["channel"]
            events.append((scene["at_minute"], channel, scene["speaker"], scene["message"]))
    for m in case["messages"]:
        channel = "assistant" if m["channel"].startswith("assistant") else m["channel"]
        events.append((m["minute"], channel, SENDER[m["channel"]], m["text"]))
    for minute, channel, sender, text in sorted(events, key=lambda e: e[0]):
        store.add_message(sid, channel, sender, text, minute)
    for part, text in case.get("plan", {}).items():
        store.upsert_part(sid, part, text, 100)
    return sid


def answers_of(results: list[dict]) -> dict[tuple[str, str], bool]:
    return {(r["element"], c["check_id"]): c["answer"] == "yes"
            for r in results for c in r.get("checks", [])}


async def main(repeat: int) -> str:
    cases = tomllib.loads((ROOT / "eval" / "cases.toml").read_text(encoding="utf-8"))["case"]
    persona_elements = [e["name"] for e in settings.observation_spec()["element"]]
    lines = ["# 관찰 판별 평가 결과", "",
             f"- 사례 {len(cases)}개, B(하이브리드) {repeat}회 반복(캐시 없음), A(규칙만) 1회",
             "- 체크 정확도: 정답 라벨과 같은 비율 / 일관성: 반복 결과가 모두 같은 비율",
             "- 요소 정확도: '관련 행동이 있었나'를 요소 단위로 정답과 비교 (A·B 공통 기준)", ""]
    totals = {"check_ok": 0, "check_n": 0, "consistent": 0, "a_ok": 0, "b_ok": 0, "el_n": 0}
    rows = []
    for case in cases:
        sid = load_case(case)
        expected = {(el, cid): v for el, checks in case["expected"].items() for cid, v in checks.items()}
        runs = [answers_of(await observe.observe_session(sid, use_cache=False)) for _ in range(repeat)]
        rules = await observe.observe_session(sid, mode="rules")

        check_ok = sum(runs[0].get(k, False) == v for k, v in expected.items())
        consistent = sum(len({run.get(k, False) for run in runs}) == 1 for k in expected)
        a_ok = b_ok = 0
        for el in persona_elements:
            truth = any(v for (e, _), v in expected.items() if e == el)
            b_pred = any(v for (e, _), v in runs[0].items() if e == el)
            a_pred = any(c["answer"] == "yes" for r in rules if r["element"] == el for c in r["checks"])
            a_ok += a_pred == truth
            b_ok += b_pred == truth
        misses = [f"{e}.{c}" for (e, c), v in expected.items() if runs[0].get((e, c), False) != v]
        rows.append(f"| {case['name']} | {check_ok}/{len(expected)} | {consistent}/{len(expected)} | "
                    f"{a_ok}/{len(persona_elements)} | {b_ok}/{len(persona_elements)} | "
                    f"{', '.join(misses) or '-'} |")
        for key, val in [("check_ok", check_ok), ("check_n", len(expected)), ("consistent", consistent),
                         ("a_ok", a_ok), ("b_ok", b_ok), ("el_n", len(persona_elements))]:
            totals[key] += val

    lines += ["| 사례 | B 체크 정확도 | B 일관성 | A 요소 정확도 | B 요소 정확도 | B가 틀린 체크 |",
              "|---|---|---|---|---|---|", *rows,
              f"| **합계** | **{totals['check_ok']}/{totals['check_n']}** | "
              f"**{totals['consistent']}/{totals['check_n']}** | **{totals['a_ok']}/{totals['el_n']}** | "
              f"**{totals['b_ok']}/{totals['el_n']}** | |"]
    report = "\n".join(lines) + "\n"
    (ROOT / "eval" / "results.md").write_text(report, encoding="utf-8")
    return report


if __name__ == "__main__":
    print(asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 3)))
