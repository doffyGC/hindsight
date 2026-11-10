from pathlib import Path

import pytest

from hindsight.review import run_review
from hindsight.store import Outcome, Stage, Store, Task


@pytest.fixture
def store(tmp_path: Path):
    with Store.open(tmp_path / "h.sqlite") as store:
        for sha, date, statement in [("bbb", "2026-11-02", None), ("aaa", "2026-11-01", "Fix it.")]:
            store.upsert_task(Task(sha, "p", date, f"subject {sha}", f2p_tests=["t::x"]))
            if statement:
                store.set_statement(sha, statement, "commit")
            store.log(sha, Stage.FAIL_TO_PASS, Outcome.PASSED)
        store.upsert_task(Task("ccc", "p", "2026-11-03", "rejected task"))
        store.log("ccc", Stage.FAIL_TO_PASS, Outcome.DISCARDED, "no fail-to-pass test")
        yield store


def _review(store: Store, tmp_path: Path, answers: list[str], edited: str | None = None):
    output: list[str] = []
    replies = iter(answers)
    summary = run_review(
        store,
        lambda sha: tmp_path / sha,
        prompt=lambda _: next(replies),
        echo=output.append,
        editor=lambda text: edited,
        reviewer="gui",
        github_repo="o/r",
    )
    return summary, "\n".join(output)


def test_only_accepted_tasks_oldest_first(store: Store) -> None:
    assert [t.commit_sha for t in store.accepted_tasks()] == ["aaa", "bbb"]


def test_approve_and_discard(store: Store, tmp_path: Path) -> None:
    summary, output = _review(store, tmp_path, ["a", "d"])
    assert (summary.approved, summary.discarded) == (1, 1)
    assert "https://github.com/o/r/commit/aaa" in output
    assert store.get_task("aaa").curation_status == "approved"
    assert store.get_task("bbb").curation_status == "discarded"
    assert store.curation_info("aaa")[0] == "gui"
    review = next(r for r in store.funnel() if r.stage == Stage.REVIEW)
    assert (review.passed, review.discarded) == (1, 1)


def test_cannot_approve_without_statement(store: Store, tmp_path: Path) -> None:
    summary, output = _review(store, tmp_path, ["s", "a", "s"])
    assert "no statement; edit it first" in output
    assert summary.approved == 0 and summary.skipped == 2


def test_edit_marks_statement_as_human(store: Store, tmp_path: Path) -> None:
    summary, _ = _review(store, tmp_path, ["e", "a", "q"], edited="  Exponent indexes must fail.  ")
    task = store.get_task("aaa")
    assert (task.statement, task.statement_source) == ("Exponent indexes must fail.", "human")
    assert summary.approved == 1 and summary.stopped_early


def test_quit_leaves_tasks_pending(store: Store, tmp_path: Path) -> None:
    summary, _ = _review(store, tmp_path, ["q"])
    assert summary.stopped_early
    assert len(store.accepted_tasks(curation_status="pending")) == 2


def test_view_shows_reference_patch_and_invalid_choice(store: Store, tmp_path: Path) -> None:
    (tmp_path / "aaa" / "reference").mkdir(parents=True)
    (tmp_path / "aaa" / "reference" / "reference.patch").write_text("+x = 2\n")
    _, output = _review(store, tmp_path, ["v", "z", "q"])
    assert "+x = 2" in output
    assert "Please choose one of" in output


def test_empty_queue(tmp_path: Path) -> None:
    with Store.open(tmp_path / "empty.sqlite") as store:
        summary, output = _review(store, tmp_path, [])
    assert "No tasks waiting for review." in output and summary.approved == 0
