import subprocess
from pathlib import Path

import pytest

from hindsight.config import DEFAULT_AI_PATTERNS
from hindsight.mining.candidates import list_candidates
from hindsight.mining.snapshot import SnapshotError, build_snapshot

from .conftest import commit


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture
def candidate(repo: Path):
    commit(repo, {"CLAUDE.md": "rules", "pkg/AGENTS.md": "more rules"}, "add instructions")
    commit(
        repo,
        {"pkg/core.py": "x = 2\n", "tests/test_core.py": "def test(): assert x == 2\n"},
        "fix: x should be 2",
    )
    commit(repo, {"pkg/core.py": "x = 3\n", "tests/test_core.py": "# future\n"}, "later change")
    found = list_candidates(repo, None, ["tests"], DEFAULT_AI_PATTERNS)
    return next(c for c in found if c.subject == "fix: x should be 2")


def test_workdir_is_parent_tree(repo: Path, candidate, tmp_path: Path) -> None:
    snap = build_snapshot(repo, candidate, tmp_path / "task")
    assert (snap.workdir / "pkg/core.py").read_text() == "x = 1\n"
    assert (snap.workdir / "tests/test_core.py").read_text() == "def test(): pass\n"


def test_workdir_has_no_future_history(repo: Path, candidate, tmp_path: Path) -> None:
    snap = build_snapshot(repo, candidate, tmp_path / "task")
    log = _git(snap.workdir, "log", "--oneline").splitlines()
    assert len(log) == 1
    assert candidate.sha not in _git(snap.workdir, "log", "--all", "--format=%H")
    assert _git(snap.workdir, "status", "--porcelain") == ""


def test_snapshot_commit_is_reproducible(repo: Path, candidate, tmp_path: Path) -> None:
    first = build_snapshot(repo, candidate, tmp_path / "a")
    second = build_snapshot(repo, candidate, tmp_path / "b")
    assert _git(first.workdir, "rev-parse", "HEAD") == _git(second.workdir, "rev-parse", "HEAD")


def test_strips_instruction_files_at_any_depth(repo: Path, candidate, tmp_path: Path) -> None:
    snap = build_snapshot(
        repo, candidate, tmp_path / "task", strip_files=["CLAUDE.md", "AGENTS.md"]
    )
    assert snap.stripped_files == ("CLAUDE.md", "pkg/AGENTS.md")
    assert not (snap.workdir / "CLAUDE.md").exists()
    assert not (snap.workdir / "pkg/AGENTS.md").exists()


def test_gold_tests_come_from_the_commit(repo: Path, candidate, tmp_path: Path) -> None:
    snap = build_snapshot(repo, candidate, tmp_path / "task")
    assert snap.gold_test_files == ("tests/test_core.py",)
    assert (snap.gold_dir / "tests/test_core.py").read_text() == "def test(): assert x == 2\n"


def test_rebuild_replaces_previous_snapshot(repo: Path, candidate, tmp_path: Path) -> None:
    dest = tmp_path / "task"
    build_snapshot(repo, candidate, dest)
    (dest / "work" / "agent_leftover.txt").write_text("stale")
    build_snapshot(repo, candidate, dest)
    assert not (dest / "work" / "agent_leftover.txt").exists()


def test_commit_that_only_deletes_tests_is_rejected(repo: Path, tmp_path: Path) -> None:
    commit(repo, {"tests/test_extra.py": "def test(): pass\n"}, "add extra test")
    (repo / "tests/test_extra.py").unlink()
    commit(repo, {"pkg/core.py": "x = 9\n"}, "drop extra test")
    found = list_candidates(repo, None, ["tests"], DEFAULT_AI_PATTERNS)
    drop = next(c for c in found if c.subject == "drop extra test")
    with pytest.raises(SnapshotError, match="only deletes test files"):
        build_snapshot(repo, drop, tmp_path / "task")


def test_strips_symlinked_instruction_files(repo: Path, tmp_path: Path) -> None:
    commit(repo, {"AGENTS.md": "rules"}, "add agents")
    (repo / "CLAUDE.md").symlink_to("AGENTS.md")  # same layout as sqlglot
    commit(repo, {}, "link claude")
    commit(repo, {"pkg/core.py": "x = 5\n", "tests/test_core.py": "# 5\n"}, "fix")
    found = list_candidates(repo, None, ["tests"], DEFAULT_AI_PATTERNS)
    fix = next(c for c in found if c.subject == "fix")

    snap = build_snapshot(repo, fix, tmp_path / "task", strip_files=["CLAUDE.md", "AGENTS.md"])

    assert snap.stripped_files == ("AGENTS.md", "CLAUDE.md")
    assert not (snap.workdir / "CLAUDE.md").is_symlink()
    assert "CLAUDE.md" not in _git(snap.workdir, "ls-files")


def test_reference_patch_holds_only_source_changes(repo: Path, candidate, tmp_path: Path) -> None:
    snap = build_snapshot(repo, candidate, tmp_path / "task")
    patch = snap.reference_patch.read_text()
    assert "pkg/core.py" in patch and "+x = 2" in patch
    assert "tests/" not in patch
    applied = subprocess.run(
        ["git", "-C", str(snap.workdir), "apply", "--check", str(snap.reference_patch)],
        capture_output=True,
    )
    assert applied.returncode == 0


def test_reference_patch_skips_stripped_files(repo: Path, tmp_path: Path) -> None:
    commit(repo, {"CLAUDE.md": "v1"}, "add rules")
    commit(repo, {"CLAUDE.md": "v2", "pkg/core.py": "x = 7\n", "tests/test_core.py": "#"}, "fix")
    found = list_candidates(repo, None, ["tests"], DEFAULT_AI_PATTERNS)
    fix = next(c for c in found if c.subject == "fix")
    snap = build_snapshot(repo, fix, tmp_path / "task", strip_files=["CLAUDE.md"])
    assert "CLAUDE.md" not in snap.reference_patch.read_text()


def test_export_tree(repo: Path, tmp_path: Path) -> None:
    from hindsight.mining.snapshot import export_tree

    tree = export_tree(repo, "HEAD", tmp_path / "src")
    assert (tmp_path / "src" / "pkg" / "core.py").read_text() == "x = 1\n"
    assert len(tree) == 40


def test_unremovable_previous_snapshot_becomes_a_discard(
    repo: Path, candidate, tmp_path: Path, monkeypatch
) -> None:
    import shutil

    dest = tmp_path / "task"
    build_snapshot(repo, candidate, dest)

    def locked(path, *args, **kwargs):
        raise PermissionError(13, "Permission denied", str(dest / "gold"))

    monkeypatch.setattr(shutil, "rmtree", locked)
    with pytest.raises(SnapshotError, match="leftover Docker container"):
        build_snapshot(repo, candidate, dest)
