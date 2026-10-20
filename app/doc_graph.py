"""회사 문서 등록 Graph (F-06, Harness: Guardrails 승인 · State·Memory 재개).

    START → prepare → approval ─(interrupt: 여기서 멈추고 상태 저장)─┐
                                                                    │ 관리자가 승인/거절
                       ┌──────── Command(resume=...) 로 같은 지점에서 재개 ┘
                       ├─ 승인 → register: rag-mcp register_company_doc (승인 토큰 필수) → END
                       └─ 거절 → reject → END

- prepare: 형식 검사, 개인정보(이메일·전화번호) 마스킹. 승인자는 마스킹된 본문을 미리 본다.
- 체크포인터에 thread_id(=요청 id)별로 상태를 저장해, 승인 요청이 다른 HTTP 요청으로 와도 이어진다.
  MVP는 메모리 체크포인터라 서버를 재시작하면 대기 중 요청은 사라진다(README에 기록).
"""
import re
import uuid
from typing import TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from app import approval, store
from app.mcp_client import call_rag

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE = re.compile(r"\b01[016789][-\s]?\d{3,4}[-\s]?\d{4}\b")
DOC_ID = re.compile(r"[a-z0-9_-]{2,40}")


class DocState(TypedDict, total=False):
    request_id: str
    company: str
    doc_id: str
    title: str
    body: str
    masked_count: int
    approved: bool
    result: str


def mask(text: str) -> tuple[str, int]:
    """이메일·휴대폰 번호를 가린다(개인정보 마스킹). (가린 본문, 가린 개수)."""
    text, n1 = EMAIL.subn("[이메일 가림]", text)
    text, n2 = PHONE.subn("[전화번호 가림]", text)
    return text, n1 + n2


def prepare(state: DocState) -> DocState:
    if not DOC_ID.fullmatch(state["doc_id"]):
        raise ValueError("문서 id는 영문 소문자·숫자·-·_ 2~40자로 써 주세요.")
    if not state["title"].strip() or not state["body"].strip():
        raise ValueError("문서 제목과 본문을 입력해 주세요.")
    body, n = mask(state["body"])
    store.save_doc_request(state["request_id"], state["company"], state["doc_id"],
                           state["title"].strip(), body, n)
    return {"body": body, "masked_count": n, "title": state["title"].strip()}


def wait_approval(state: DocState) -> DocState:
    """사람의 결정을 기다린다. interrupt가 Graph를 멈추고, 재개될 때 결정 값을 돌려준다."""
    decision = interrupt({"request_id": state["request_id"], "doc_id": state["doc_id"],
                          "title": state["title"], "masked_count": state["masked_count"]})
    return {"approved": bool(decision.get("approved"))}


def after_approval(state: DocState) -> str:
    return "register" if state["approved"] else "reject"


async def register(state: DocState) -> DocState:
    """승인된 내용에만 토큰을 발급해 rag-mcp 변경 도구를 부른다."""
    token = approval.issue(state["request_id"], state["company"], state["doc_id"], state["body"])
    result = await call_rag("register_company_doc", {
        "request_id": state["request_id"], "company": state["company"], "doc_id": state["doc_id"],
        "title": state["title"], "body": state["body"], "approval_token": token})
    text = str(result)
    status = "rejected" if "Error" in text else "registered"
    store.set_doc_request_status(state["request_id"], status)
    return {"result": status}


def reject(state: DocState) -> DocState:
    store.set_doc_request_status(state["request_id"], "rejected")
    return {"result": "rejected"}


def build():
    g = StateGraph(DocState)
    for name, fn in [("prepare", prepare), ("approval", wait_approval), ("register", register),
                     ("reject", reject)]:
        g.add_node(name, fn)
    g.add_edge(START, "prepare")
    g.add_edge("prepare", "approval")
    g.add_conditional_edges("approval", after_approval, ["register", "reject"])
    g.add_edge("register", END)
    g.add_edge("reject", END)
    return g.compile(checkpointer=InMemorySaver())


DOC_GRAPH = build()


async def request(company: str, doc_id: str, title: str, body: str) -> dict:
    """등록 요청: 승인 대기 지점까지 실행하고 멈춘다. 미리보기 정보를 돌려준다."""
    request_id = uuid.uuid4().hex[:12]
    cfg = {"configurable": {"thread_id": request_id}}
    await DOC_GRAPH.ainvoke({"request_id": request_id, "company": company, "doc_id": doc_id,
                             "title": title, "body": body}, cfg)
    snap = DOC_GRAPH.get_state(cfg)
    return {"request_id": request_id, "waiting": bool(snap.next),
            "preview": snap.values["body"], "masked_count": snap.values["masked_count"]}


async def decide(request_id: str, company: str, approved: bool) -> str:
    """승인/거절: 멈춘 지점에서 Graph를 재개한다.

    Raises:
        KeyError: 없는 요청이거나 다른 기업 요청.
        RuntimeError: 이미 처리됐거나(서버 재시작 등으로) 대기 상태가 없는 요청.
    """
    req = store.get_doc_request(request_id)
    if req is None or req["company"] != company:
        raise KeyError("없는 요청입니다.")
    cfg = {"configurable": {"thread_id": request_id}}
    if not DOC_GRAPH.get_state(cfg).next:
        raise RuntimeError("이미 처리됐거나 대기 중이 아닌 요청입니다.")
    out = await DOC_GRAPH.ainvoke(Command(resume={"approved": approved}), cfg)
    return out["result"]
