"""시나리오 팩 구조 검사: 팩마다 이야기는 달라도 체크 목록과 장면 뼈대는 같아야 한다.

코드(graph·observe·clock)가 장면 id와 체크 id를 직접 쓰므로, 새 팩이 이를 빠뜨리면 세션 중에야 터진다.
"""
import pytest

from app import settings

PACKS = settings.pack_ids()
# 코드와 관찰 구간이 기대는 장면 id(app/graph.py negotiate, app/clock.py deadline_report, observation.toml scene)
REQUIRED_SCENES = {"kickoff", "negotiate", "coworker_miss", "lead_checkin", "pivot",
                   "status_narration", "report_request", "deadline_report"}


def _check_signature(pack: str) -> dict:
    """역량 이름 → (체크 id·방향 집합, 규칙 관찰 id 집합)."""
    spec = settings.observation_spec(pack)
    return {e["name"]: ({(c["id"], c.get("direction", "positive")) for c in e["checks"]},
                        {r["id"] for r in e.get("rules", [])})
            for e in spec["element"]}


def test_has_more_than_one_pack():
    assert len(PACKS) >= 2


@pytest.mark.parametrize("pack", PACKS)
def test_same_checks_as_default_pack(pack):
    assert _check_signature(pack) == _check_signature(settings.default_pack())


@pytest.mark.parametrize("pack", PACKS)
def test_required_scenes_exist(pack):
    ids = [s["id"] for s in settings.scenes(pack)]
    assert REQUIRED_SCENES <= set(ids)
    assert len(ids) == len(set(ids))   # 장면 id 중복 없음


@pytest.mark.parametrize("pack", PACKS)
def test_scene_skeleton_order(pack):
    """관찰 구간이 기대는 순서: 분담 → 동료 문제 → 조건 변경 → 보고 요청 → 종료."""
    minute = {s["id"]: s["at_minute"] for s in settings.scenes(pack)}
    order = ["negotiate", "coworker_miss", "pivot", "report_request", "deadline_report"]
    assert [minute[s] for s in order] == sorted(minute[s] for s in order)


@pytest.mark.parametrize("pack", PACKS)
def test_references_point_to_existing_scenes(pack):
    ids = {s["id"] for s in settings.scenes(pack)}
    elements = {e["name"] for e in settings.observation_spec(pack)["element"]}
    for e in settings.observation_spec(pack)["element"]:
        assert e["scene"] in ids
        assert e.get("window_end_scene", "kickoff") in ids
    for s in settings.scenes(pack):
        assert not s["element"] or s["element"] in elements
        for key in ("skip_if_candidate_wrote", "early_after_consult"):
            if key in s:
                assert s[key]["since_scene"] in ids
    work = settings.work_spec(pack)
    for item in work.get("stage", []) + work.get("effect", []):
        assert item["after_scene"] in ids


@pytest.mark.parametrize("pack", PACKS)
def test_parts_and_docs_consistent(pack):
    parts = set(settings.part_titles(pack))
    info = settings.pack_info(pack)
    assert set(info["part_units"]) == parts == set(settings.default_assignment(pack))
    assert {e["part"] for e in settings.work_spec(pack).get("effect", [])} <= parts
    for d in settings.docs_index(pack):
        assert settings.doc_text(d["id"], pack).strip()
