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


def test_promise_without_tool_call_is_deferral():
    # "읽어보고 피드백 드릴게요"라고만 하고 이번 답에서 도구를 한 번도 안 부르면 실패
    text = "네, 공유 문서에서 내용을 읽어보고 피드백 드릴게요. 잠시만 기다려 주세요."
    assert sg.check(text, [], [], True, tools_used=0) == "deferral"
    assert sg.check(text, [], [], True, tools_used=1) == "ok"
    assert sg.check(text, [], [], False, tools_used=0) == "ok"     # 도구가 없는 장면 대사는 대상 아님


def test_review_request_requires_reading_shared_plan():
    # 검토 요청에는 공유 문서를 실제로 읽어야 한다(읽지 않고 "확인했습니다"만 하면 실패)
    text = "3번 작성해 주신 부분 확인했습니다! 4·5번 작업할게요."
    assert sg.check(text, [], [], True, tools_used=1, called_tools=["search_company_docs"],
                    required_tool="read_shared_plan") == "not_read"
    assert sg.check(text, [], [], True, tools_used=1, called_tools=["read_shared_plan"],
                    required_tool="read_shared_plan") == "ok"


def test_review_request_detection():
    from app import persona
    assert persona.is_review_request("3번 보완할 부분 있으면 수정해주시고")
    assert persona.is_review_request("제가 쓴 1번 한번 봐주실 수 있을까요?")
    assert not persona.is_review_request("사례는 어떤 기준으로 고를까요?")


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


def test_progress_events_follow_nodes():
    from app import progress
    sg.verify_node({"session_id": "s1", "node": "persona.coworker", "text": "네", "must_include": [],
                    "read_titles": [], "use_tools": False, "minute": 0, "company": "",
                    "tool_calls": 0, "called_tools": [], "required_tool": "", "verdicts": [], "regenerations": 0})
    assert progress.get("s1") == [{"actor": "coworker", "label": "답을 확인하는 중", "node": "verify"}]
    sg.done_node({"session_id": "s1", "node": "persona.coworker", "text": "네"})
    assert progress.get("s1") == []


def test_giving_up_without_lookup_is_no_lookup():
    text = "죄송하지만 그 정보는 제공할 수 없습니다."
    assert sg.check(text, [], [], True, tools_used=0) == "no_lookup"
    assert sg.check(text, [], [], True, tools_used=1) == "ok"      # 찾아보고도 없으면 그렇게 말해도 됨


def test_coworker_claiming_unshared_work_is_caught():
    # 공유(01:30) 전인데 "정리해봤는데"처럼 결과물을 이미 만든 것처럼 말하면 실패
    text = "지금 비슷한 서비스 사례를 찾고 있어요. 몇 가지 사례를 정리해봤는데 같이 볼까요?"
    assert sg.check(text, [], [], True, tools_used=0, unshared=True) == "unshared_claim"
    assert sg.check("지금 4번 사례를 찾는 중이에요.", [], [], True, tools_used=0, unshared=True) == "ok"
    assert sg.check(text, [], [], True, tools_used=0, unshared=False) == "ok"   # 공유 뒤에는 대상 아님


# 회귀: 팀장이 01:10·01:50에도 킥오프의 "2시간 뒤"를 반복했다 → 코드가 잡고 지금 시각을 알려 다시 쓰게 한다
def test_stale_deadline_is_rejected_after_kickoff():
    assert sg.check("2시간 뒤 기획안을 받겠습니다", [["2시간"]], [], False, minute=0) == "ok"   # 킥오프는 정상
    verdict = sg.check("마감은 그대로 2시간 뒤입니다", [], [], False, minute=70, scenario="pixel")
    assert verdict == "stale_time:마감 01:50, 남은 시간 약 40분"
    assert sg.check("마감은 그대로예요", [], [], False, minute=70, scenario="pixel") == "ok"
    assert sg.check("2시간 뒤까지 제출해 주세요", [], [], False, minute=110).startswith("stale_time:마감 시각(01:50)이 됐다")
