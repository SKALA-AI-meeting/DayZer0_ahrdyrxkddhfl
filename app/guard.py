"""Guardrails: 판정 발언 금지(F-24).

입력 쪽: 평가 기준 질문은 LLM에 넘기지 않고 정해진 안내문으로 답한다.
출력 쪽: AI 응답에 판정 표현이 있으면 다시 생성하거나 안내문으로 바꾼다(호출하는 쪽에서 처리).
"""
from app.settings import config, prompt


def is_evaluation_question(text: str) -> bool:
    """지원자 메시지가 평가 기준·결과를 묻는지 키워드로 판단한다."""
    return any(k in text for k in config()["guard"]["evaluation_question_keywords"])


def violates(text: str) -> bool:
    """AI 응답에 판정·연출 관련 금지 표현이 있는지 확인한다."""
    return any(k in text for k in config()["guard"]["forbidden_output_keywords"])


def notice() -> str:
    """평가 기준 질문에 답하는 정해진 안내문."""
    return prompt("guard")
