"""Interactive curation of task statements (RF-09 to RF-11)."""

from __future__ import annotations

import getpass
import os
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from hindsight.mining.interface import gold_test_sources, required_new_names
from hindsight.mining.statements import leakage_hints
from hindsight.store import Store, Task

Prompt = Callable[[str], str]
Echo = Callable[[str], None]
Editor = Callable[[str], str | None]

CHOICES = "[a]pprove  [e]dit  [d]iscard  [t]ests  [v]iew fix  [s]kip  [q]uit"


@dataclass
class ReviewSummary:
    approved: int = 0
    discarded: int = 0
    skipped: int = 0
    stopped_early: bool = False


def open_in_editor(text: str) -> str | None:
    """Edit `text` in $VISUAL or $EDITOR (falling back to nano, then vi); None if unchanged."""
    command = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not command:
        command = next((name for name in ("nano", "vi") if shutil.which(name)), None)
    if not command:
        raise RuntimeError("no editor found; set the EDITOR environment variable")
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as tmp:
        tmp.write(text)
        path = Path(tmp.name)
    try:
        subprocess.run([*shlex.split(command), str(path)], check=True)
        edited = path.read_text(encoding="utf-8")
    finally:
        path.unlink(missing_ok=True)
    return None if edited == text else edited


def reviewer_name(repo: Path | None = None) -> str:
    try:
        args = (
            ["git", "config", "user.name"]
            if repo is None
            else ["git", "-C", str(repo), "config", "user.name"]
        )
        name = subprocess.run(args, capture_output=True, text=True, check=True).stdout.strip()
        if name:
            return name
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    return getpass.getuser()


def _read(path: Path) -> str:
    return path.read_text(errors="replace") if path.exists() else ""


def _gold_text(task_dir: Path, task: Task) -> str:
    """Everything the tests reveal: file contents, file paths and fail-to-pass test names."""
    contents = [_read(task_dir / "gold" / name) for name in task.gold_test_files]
    return "\n".join([*contents, *task.gold_test_files, *task.f2p_tests])


def _show(
    task: Task,
    task_dir: Path,
    index: int,
    total: int,
    echo: Echo,
    link: str | None,
    required: list[str],
):
    echo("")
    echo(f"── Task {index}/{total} · {task.commit_sha[:10]} · {task.commit_date}")
    echo(task.subject)
    if link:
        echo(link)
    echo(f"Fail-to-pass tests ({len(task.f2p_tests)}):")
    for test in task.f2p_tests[:10]:
        echo(f"  - {test}")
    if len(task.f2p_tests) > 10:
        echo(f"  … and {len(task.f2p_tests) - 10} more")
    echo(f"\nStatement (source: {task.statement_source or 'none'}):")
    if task.statement:
        for line in task.statement.splitlines():
            echo(f"  {line}")
        hints = leakage_hints(
            task.statement,
            _read(task_dir / "reference" / "reference.patch"),
            _gold_text(task_dir, task),
        )
        if hints:
            echo("\nPossible leakage of the solution:")
            for hint in hints:
                echo(f"  ! {hint}")
    else:
        echo("  (no statement yet: edit to write one)")
    missing = [n for n in required if n not in (task.statement or "")]
    if missing:
        echo("\nThe tests need names that do not exist yet; the statement should mention them:")
        for name in missing:
            echo(f"  ? {name}")


def run_review(
    store: Store,
    task_dir_for: Callable[[str], Path],
    prompt: Prompt,
    echo: Echo,
    editor: Editor,
    reviewer: str,
    github_repo: str | None = None,
    test_paths: tuple[str, ...] = ("tests",),
) -> ReviewSummary:
    tasks = store.accepted_tasks(curation_status="pending")
    summary = ReviewSummary()
    if not tasks:
        echo("No tasks waiting for review.")
        return summary

    for index, task in enumerate(tasks, start=1):
        task_dir = task_dir_for(task.commit_sha)
        link = f"https://github.com/{github_repo}/commit/{task.commit_sha}" if github_repo else None
        required = required_new_names(task_dir, task.gold_test_files, test_paths)
        _show(task, task_dir, index, len(tasks), echo, link, required)
        while True:
            choice = prompt(CHOICES).strip().lower()[:1]
            if choice == "a":
                if not (task.statement or "").strip():
                    echo("This task has no statement; edit it first.")
                    continue
                store.set_curation(task.commit_sha, "approved", reviewer)
                store.commit()
                summary.approved += 1
                break
            if choice == "e":
                edited = editor(task.statement or "")
                if edited is not None and edited.strip() and edited.strip() != task.statement:
                    task.statement = edited.strip()
                    task.statement_source = "human"
                    store.set_statement(task.commit_sha, task.statement, "human")
                    store.commit()
                _show(task, task_dir, index, len(tasks), echo, link, required)
                continue
            if choice == "d":
                store.set_curation(task.commit_sha, "discarded", reviewer)
                store.commit()
                summary.discarded += 1
                break
            if choice == "t":
                echo(gold_test_sources(task_dir, task.f2p_tests, task.gold_test_files))
                continue
            if choice == "v":
                echo(_read(task_dir / "reference" / "reference.patch") or "(empty patch)")
                continue
            if choice == "s":
                summary.skipped += 1
                break
            if choice == "q":
                summary.stopped_early = True
                return summary
            echo("Please choose one of: a, e, d, t, v, s, q.")
    return summary
