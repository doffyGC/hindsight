from pathlib import Path

import pytest

from hindsight.store import Outcome, Stage, Store, Task


def test_funnel_counts_and_reasons(tmp_path: Path) -> None:
    with Store.open(tmp_path / "h.sqlite") as store:
        for sha in ("a", "b", "c"):
            store.log(sha, Stage.CANDIDATE, Outcome.PASSED)
        store.log("a", Stage.AI_FILTER, Outcome.PASSED)
        store.log("b", Stage.AI_FILTER, Outcome.PASSED)
        store.log("c", Stage.AI_FILTER, Outcome.DISCARDED, "AI-assisted commit")
        rows = store.funnel()
    funnel = {row.stage: row for row in rows}

    assert funnel[Stage.CANDIDATE].passed == 3
    assert funnel[Stage.AI_FILTER].passed == 2
    assert funnel[Stage.AI_FILTER].discarded == 1
    assert funnel[Stage.AI_FILTER].reasons == {"AI-assisted commit": 1}
    assert [row.stage for row in rows] == list(Stage)


def test_relogging_a_stage_overwrites_it(tmp_path: Path) -> None:
    with Store.open(tmp_path / "h.sqlite") as store:
        store.log("a", Stage.SNAPSHOT, Outcome.DISCARDED, "boom")
        store.log("a", Stage.SNAPSHOT, Outcome.PASSED)
        row = next(r for r in store.funnel() if r.stage == Stage.SNAPSHOT)
    assert (row.passed, row.discarded) == (1, 0)


def test_task_roundtrip_and_upsert(tmp_path: Path) -> None:
    path = tmp_path / "h.sqlite"
    task = Task("abc", "def", "2026-11-02", "fix: x", gold_test_files=["tests/t.py"])
    with Store.open(path) as store:
        store.upsert_task(task)
        task.snapshot_path = "/tmp/snap"
        store.upsert_task(task)
    with Store.open(path) as store:  # persisted across connections
        loaded = store.get_task("abc")
        assert store.count_tasks() == 1
    assert loaded == task
    assert loaded.curation_status == "pending"


def test_unknown_task_is_none(tmp_path: Path) -> None:
    with Store.open(tmp_path / "h.sqlite") as store:
        assert store.get_task("nope") is None


def test_rejects_unknown_schema_version(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "h.sqlite"
    sqlite3.connect(path).execute("PRAGMA user_version = 99").connection.close()
    with pytest.raises(RuntimeError, match="schema v99"):
        with Store.open(path):
            pass
