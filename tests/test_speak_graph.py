"""발화 Graph 검증 로직 테스트 (LLM 없이 코드 검증 부분과 분기만)."""
from langchain_core.messages import AIMessage

from app import speak_graph as sg
from app.settings import scenes


def test_check_judgement_missing_and_citation():
    assert sg.check("지원자님은 합격권이에요", [], [], False) == "judgement"
    assert sg.check("대상을 바꿔 주세요", [["3개월"], ["마감"]], [], False) == "missing:3개월"
    # 읽지 않은 문서를 근거로 대면 출처 실패
    assert sg.check("운영 정책 문서에 따르면 유료는 안 돼요", [], [], True) == "citation:운영 정책"
    assert sg.check("운영 정책 문서에 따르면 유료는 안 돼요", [], ["운영 정책"], True) == "ok"
    # 도구를 쓰지 않는 장면 대사에는 출처 검사를 하지 않는다
    assert sg.check("운영 정책 문서 참고하세요", [], [], False) == "ok"


def test_saying_unreleased_doc_is_unavailable_is_not_citation_error():
    # 00:55에 "인터뷰 요약은 아직 볼 수 없다"고 말하는 건 근거 인용이 아니다(공개 전 문서는 검사 대상 아님)
    text = "'사용자 인터뷰 요약' 문서는 아직 공유되지 않아 볼 수 없어요."
    assert sg.check(text, [], [], True, minute=55) == "ok"
    # 01:05 이후 공개된 뒤에 읽지 않고 근거로 대면 실패
    assert sg.check("사용자 인터뷰 요약에 따르면 어색함이 문제예요", [], [], True, minute=66) \
        == "citation:사용자 인터뷰 요약"


def test_every_scene_fallback_passes_its_own_check():
    # 대체 문장 자체가 검증을 통과해야 한다(F-23 정해진 내용 보장)
    for s in scenes():
        if s["speaker"] != "system":
            assert sg.check(s["fallback"], s["must_include"], [], False) == "ok", s["id"]


def test_routing_after_llm_and_verify(monkeypatch):
    with_call = {"messages": [AIMessage("", tool_calls=[{"name": "list_company_docs", "args": {}, "id": "1"}])]}
    assert sg.after_llm(with_call) == "tools"
    assert sg.after_llm({"messages": [AIMessage("답")]}) == "verify"
    assert sg.after_verify({"verdict": "ok", "regenerations": 0}) == "done"
    assert sg.after_verify({"verdict": "missing:마감", "regenerations": 0}) == "feedback"
    assert sg.after_verify({"verdict": "missing:마감", "regenerations": 2}) == "fallback"


def test_fallback_uses_notice_for_judgement():
    out = sg.fallback_node({"verdict": "judgement", "fallback": "대체"})
    assert "평가 방식은" in out["text"]
    assert sg.fallback_node({"verdict": "citation:지표 현황", "fallback": "대체"})["text"] == "대체"
