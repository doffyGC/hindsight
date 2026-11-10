from pathlib import Path

from hindsight.config import DEFAULT_AI_PATTERNS
from hindsight.mining.candidates import list_candidates

from .conftest import commit


def _list(repo: Path):
    return list_candidates(repo, None, ["tests"], DEFAULT_AI_PATTERNS)


def test_keeps_commits_touching_tests_and_source(repo: Path) -> None:
    commit(repo, {"pkg/core.py": "x = 2\n", "tests/test_core.py": "def test(): assert 1\n"}, "fix")
    found = _list(repo)
    assert [c.subject for c in found] == ["fix"]
    assert found[0].test_files == ("tests/test_core.py",)
    assert found[0].source_files == ("pkg/core.py",)
    assert not found[0].ai_assisted


def test_skips_test_only_and_source_only_commits(repo: Path) -> None:
    commit(repo, {"tests/test_core.py": "def test(): assert 2\n"}, "tests only")
    commit(repo, {"pkg/core.py": "x = 3\n"}, "source only")
    assert _list(repo) == []


def test_skips_root_commit(repo: Path) -> None:
    assert _list(repo) == []  # the fixture's "init" commit has no parent


def test_flags_ai_assisted_commits(repo: Path) -> None:
    commit(repo, {"pkg/core.py": "x = 4\n", "tests/test_core.py": "def t(): 1\n"}, "fix [CLAUDE]")
    assert _list(repo)[0].ai_assisted
