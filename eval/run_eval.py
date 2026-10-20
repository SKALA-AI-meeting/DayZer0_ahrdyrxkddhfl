"""관찰 판별 평가: B(하이브리드) 반복 일관성·정확도, A(규칙만)와 비교.

실행: uv run python eval/run_eval.py [반복 횟수=3] [cases | holdout]
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

from app import observe, report, settings, store  # noqa: E402  (DB 경로를 먼저 정해야 함)

def load_case(case: dict) -> str:
    """사례 하나를 새 세션으로 DB에 넣고 세션 id를 반환한다.

    장면은 scene_minutes(없으면 events.toml 시각)에 실행된 것으로 기록하고, 대사는 fallback 문장을 그대로 넣는다.
    skipped 장면은 실행하지 않고 '건너뜀' 기록만 남긴다.
    """
    sid = store.create_session(case["name"])
    overrides, skipped = case.get("scene_minutes", {}), set(case.get("skipped", []))
    events = []
    for scene in settings.scenes():
        minute = overrides.get(scene["id"], scene["at_minute"])
        store.claim_scene(sid, scene["id"], minute)
        if scene["id"] in skipped:
            store.claim_scene(sid, f"skipped:{scene['id']}", minute)
            continue
        events.append((minute, scene["channel"], scene["speaker"], scene.get("fallback") or scene["message"]))
    for m in case["messages"]:
        events.append((m["minute"], m["channel"], m.get("sender", "candidate"), m["text"]))
    for minute, channel, sender, text in sorted(events, key=lambda e: e[0]):
        store.add_message(sid, channel, sender, text, minute)
    return sid


RULE_IDS = {r["id"] for e in settings.observation_spec()["element"] for r in e.get("rules", [])}
DIRECTIONS = report.directions()


def grade_of(element: str, yes_checks: set[str]) -> str:
    """검증된 체크 id 집합 → 참고 등급(리포트와 같은 규칙, report.grade). 평가셋 사례는 모든 상황이 일어난다."""
    d = DIRECTIONS[element]
    return report.grade(True, sum(d.get(c, "positive") in ("positive", "bonus") for c in yes_checks),
                        sum(d.get(c) == "negative" for c in yes_checks),
                        sum(1 for v in d.values() if v == "positive"))


def answers_of(results: list[dict]) -> dict[tuple[str, str], bool]:
    """LLM 체크 답만(규칙 관찰은 기록으로 확정되므로 정확도 비교에서 뺀다)."""
    return {(r["element"], c["check_id"]): c["answer"] == "yes"
            for r in results for c in r.get("checks", []) if c["check_id"] not in RULE_IDS}


async def main(repeat: int, suite: str = "cases") -> str:
    """suite: cases(튜닝에 쓴 평가셋 → results.md) | holdout(튜닝에 쓰지 않은 사례 → results_holdout.md)."""
    cases = tomllib.loads((ROOT / "eval" / f"{suite}.toml").read_text(encoding="utf-8"))["case"]
    persona_elements = [e["name"] for e in settings.observation_spec()["element"]]
    title = "관찰 판별 평가 결과" if suite == "cases" else "관찰 판별 평가 결과 (홀드아웃: 체크 정의를 고칠 때 보지 않은 사례)"
    lines = [f"# {title}", "",
             f"- 사례 {len(cases)}개, B(하이브리드) {repeat}회 반복(캐시 없음), A(규칙만) 1회",
             "- 체크 정확도: 정답 라벨과 같은 비율 / 일관성: 반복 결과가 모두 같은 비율",
             "- 요소 정확도: '관련 행동이 있었나'를 요소 단위로 정답과 비교 (A·B 공통 기준)",
             "- 등급 정확도: 정답 라벨로 계산한 등급과 판별 결과로 계산한 등급이 같은 비율(같은 코드 규칙, 규칙 관찰은 실제 결과를 씀)", ""]
    totals = {"check_ok": 0, "check_n": 0, "consistent": 0, "a_ok": 0, "b_ok": 0, "el_n": 0, "g_ok": 0}
    rows = []
    for case in cases:
        sid = load_case(case)
        expected = {(el, cid): v for el, checks in case["expected"].items() for cid, v in checks.items()}
        results = [await observe.observe_session(sid, use_cache=False) for _ in range(repeat)]
        runs = [answers_of(r) for r in results]
        rules = await observe.observe_session(sid, mode="rules")
        rule_yes = {(r["element"], c["check_id"]) for r in rules for c in r["checks"]
                    if c["check_id"] in RULE_IDS and c["answer"] == "yes"}
        g_ok, g_miss = 0, []
        for el in persona_elements:
            truth_yes = {c for (e, c), v in expected.items() if e == el and v} | {c for (e, c) in rule_yes if e == el}
            pred = next(report.grade_result(r, DIRECTIONS[el])["grade"] for r in results[0] if r["element"] == el)
            truth = grade_of(el, truth_yes)
            g_ok += pred == truth
            if pred != truth:
                g_miss.append(f"{el} {truth}→{pred}")

        check_ok = sum(runs[0].get(k, False) == v for k, v in expected.items())
        consistent = sum(len({run.get(k, False) for run in runs}) == 1 for k in expected)
        a_ok = b_ok = 0
        for el in persona_elements:
            truth = any(v for (e, _), v in expected.items() if e == el)
            b_pred = any(v for (e, _), v in runs[0].items() if e == el)
            a_pred = any(c["answer"] == "yes" for r in rules if r["element"] == el for c in r["checks"]
                         if c["check_id"] == "keyword")
            a_ok += a_pred == truth
            b_ok += b_pred == truth
        misses = [f"{e}.{c}" for (e, c), v in expected.items() if runs[0].get((e, c), False) != v]
        rows.append(f"| {case['name']} | {check_ok}/{len(expected)} | {consistent}/{len(expected)} | "
                    f"{a_ok}/{len(persona_elements)} | {b_ok}/{len(persona_elements)} | "
                    f"{g_ok}/{len(persona_elements)} | {', '.join(misses) or '-'} | {', '.join(g_miss) or '-'} |")
        for key, val in [("check_ok", check_ok), ("check_n", len(expected)), ("consistent", consistent),
                         ("a_ok", a_ok), ("b_ok", b_ok), ("el_n", len(persona_elements)), ("g_ok", g_ok)]:
            totals[key] += val

    lines += ["| 사례 | B 체크 정확도 | B 일관성 | A 요소 정확도 | B 요소 정확도 | 등급 정확도 | B가 틀린 체크 | 틀린 등급(정답→예측) |",
              "|---|---|---|---|---|---|---|---|", *rows,
              f"| **합계** | **{totals['check_ok']}/{totals['check_n']}** | "
              f"**{totals['consistent']}/{totals['check_n']}** | **{totals['a_ok']}/{totals['el_n']}** | "
              f"**{totals['b_ok']}/{totals['el_n']}** | **{totals['g_ok']}/{totals['el_n']}** | | |"]
    text = "\n".join(lines) + "\n"
    (ROOT / "eval" / ("results.md" if suite == "cases" else f"results_{suite}.md")).write_text(text, encoding="utf-8")
    return text


if __name__ == "__main__":
    print(asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 3, sys.argv[2] if len(sys.argv) > 2 else "cases")))
