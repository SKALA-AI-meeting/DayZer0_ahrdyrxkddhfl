from concurrent.futures import ThreadPoolExecutor

from app import clock, settings, store


def test_clock_counts_only_active_gaps():
    sim = clock.advance(0, None, 1000.0, 1.0, 15, 120)
    sim = clock.advance(sim, 1000.0, 1010.0, 1.0, 15, 120)   # 10초 간격 → 흐름
    sim = clock.advance(sim, 1010.0, 2810.0, 1.0, 15, 120)   # 30분 끊김 → 멈춤 (F-19)
    sim = clock.advance(sim, 2810.0, 2812.0, 1.0, 15, 120)
    assert sim == 12.0


def test_clock_scale_and_cap():
    sim = clock.advance(0, 0.0, 10.0, 12.0, 15, 120)
    assert sim == 120.0
    assert clock.advance(7190.0, 0.0, 10.0, 12.0, 15, 120) == 7200.0   # 120분에서 멈춤


def test_due_scenes_in_order_without_rerun():
    due = clock.due_scenes(settings.scenes(), 50, {"kickoff", "kickoff_greeting"})
    assert [s["id"] for s in due] == ["negotiate", "coop_help", "resp_delay"]


def test_interviews_hidden_until_0105():
    ids_64 = {d["id"] for d in clock.available_docs(settings.docs_index(), 64.9)}
    ids_65 = {d["id"] for d in clock.available_docs(settings.docs_index(), 65)}
    assert "interviews" not in ids_64 and "interviews" in ids_65


def test_claim_scene_once_even_concurrently():
    sid = store.create_session("지원자A")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: store.claim_scene(sid, "coop_help", 30), range(8)))
    assert results.count(True) == 1


def test_session_and_board_defaults():
    sid = store.create_session("지원자B")
    tasks = store.list_tasks(sid)
    assert [t["owner"] for t in tasks] == ["candidate", "candidate", "both", "coworker", "coworker"]
    assert store.get_session(sid)["phase"] == "waiting"
