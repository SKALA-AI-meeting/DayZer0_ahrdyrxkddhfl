"""판정 발언 가드(F-24) 오탐 재현 테스트.

업무 대화에 흔한 표현이 평가 질문이나 판정 발언으로 잘못 걸리면,
지원자는 업무 답변 대신 "평가 방식은 시작 전 안내드린 내용이 전부예요"를 받게 된다.
"""
import pytest

from app import guard

BUSINESS_QUESTIONS = [
    "재참여율이 왜 떨어졌는지 같이 볼까요?",          # '떨어' 오탐
    "성공 지표로 만족도 점수를 넣으면 어떨까요?",       # '점수' 오탐
]
EVALUATION_QUESTIONS = ["평가 기준이 뭐예요?", "저 합격할 수 있을까요?", "제 점수는 몇 점이에요?"]
BUSINESS_ANSWERS = [
    "사용자 시나리오를 먼저 정리해 보면 좋겠어요.",     # '시나리오' 오탐
    "보조 지표로 만족도 점수 4.0 이상을 잡아 볼게요.",  # '점수' 오탐
]
JUDGEMENT_ANSWERS = ["지원자님은 합격권이에요.", "이번 평가 결과는 좋을 거예요."]


@pytest.mark.parametrize("text", BUSINESS_QUESTIONS)
def test_business_question_is_not_evaluation_question(text):
    assert not guard.is_evaluation_question(text)


@pytest.mark.parametrize("text", EVALUATION_QUESTIONS)
def test_real_evaluation_question_is_caught(text):
    assert guard.is_evaluation_question(text)


@pytest.mark.parametrize("text", BUSINESS_ANSWERS)
def test_business_answer_is_not_violation(text):
    assert not guard.violates(text)


@pytest.mark.parametrize("text", JUDGEMENT_ANSWERS)
def test_judgement_answer_is_violation(text):
    assert guard.violates(text)
