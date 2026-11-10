"""Listing candidate commits (RF-01): commits that change both tests and source code."""

from __future__ import annotations

import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from hindsight.mining.ai_commits import is_ai_assisted

_RECORD_SEP = "\x1e"
_FIELD_SEP = "\x1f"


@dataclass(frozen=True)
class Candidate:
    sha: str
    parent: str
    date: str
    subject: str
    test_files: tuple[str, ...]
    source_files: tuple[str, ...]
    ai_assisted: bool


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return result.stdout


def _is_test_path(path: str, test_paths: Iterable[str]) -> bool:
    return any(path == p or path.startswith(p.rstrip("/") + "/") for p in test_paths)


def list_candidates(
    repo: Path,
    since: str | None,
    test_paths: list[str],
    ai_patterns: list[str],
) -> list[Candidate]:
    """List non-merge commits that touch at least one test file and one non-test file."""
    fmt = _FIELD_SEP.join(["%H", "%P", "%cs", "%s", "%B"]) + _RECORD_SEP
    args = ["log", "--no-merges", f"--format={fmt}"]
    if since:
        args.append(f"--since={since}")
    args += ["--", *test_paths]

    candidates: list[Candidate] = []
    for record in _git(repo, *args).split(_RECORD_SEP):
        record = record.strip("\n")
        if not record:
            continue
        sha, parents, date, subject, body = record.split(_FIELD_SEP, 4)
        parent_list = parents.split()
        if len(parent_list) != 1:  # root commits have no parent to snapshot
            continue
        files = _git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", sha).split()
        tests = tuple(f for f in files if _is_test_path(f, test_paths))
        sources = tuple(f for f in files if not _is_test_path(f, test_paths))
        if not tests or not sources:
            continue
        candidates.append(
            Candidate(
                sha=sha,
                parent=parent_list[0],
                date=date,
                subject=subject,
                test_files=tests,
                source_files=sources,
                ai_assisted=is_ai_assisted(body, ai_patterns),
            )
        )
    return candidates
