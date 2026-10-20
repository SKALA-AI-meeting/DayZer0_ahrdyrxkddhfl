"""진행 이벤트 (평가표 AI Agent 구현: "진행 이벤트가 동작함").

발화 Graph의 노드가 지금 하는 일을 세션별로 기록하고, 화면은 /poll 응답의 activity로 받아
"이도현님이 공유 문서를 읽는 중…"처럼 보여준다. API와 Graph가 같은 프로세스에서 돌므로
메모리에 둔다(서버를 재시작하면 진행 중 표시는 사라지는 게 맞다).
"""
import time

from app import llm

_now: dict[str, dict[str, dict]] = {}   # session_id → actor → {"label", "node", "at"}


def actor_of(node: str) -> str:
    """기록용 노드 이름(persona.coworker, assistant)을 화면용 행위자(coworker, assistant)로."""
    return node.split(".")[1] if node.startswith("persona.") else node


def set(session_id: str, node: str, graph_node: str, label: str) -> None:  # noqa: A001
    """행위자가 지금 하는 일을 기록하고 trace에도 진행 이벤트로 남긴다."""
    if not session_id:
        return
    actor = actor_of(node)
    _now.setdefault(session_id, {})[actor] = {"label": label, "node": graph_node, "at": time.time()}
    llm.trace({"session": session_id, "node": f"{node}.{graph_node}", "progress": label})


def clear(session_id: str, node: str) -> None:
    _now.get(session_id, {}).pop(actor_of(node), None)


def get(session_id: str) -> list[dict]:
    """지금 진행 중인 일 목록 [{actor, label, node}]."""
    return [{"actor": a, "label": v["label"], "node": v["node"]}
            for a, v in _now.get(session_id, {}).items()]
