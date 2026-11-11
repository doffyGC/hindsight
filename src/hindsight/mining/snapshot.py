"""Task snapshots (RNF-09): the parent commit's tree in a fresh one-commit repository.

The agent works on this snapshot, so it cannot reach future history (`git log`, `git show`)
and never sees the gold tests, which are extracted separately for the evaluator.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import tarfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from hindsight.mining.candidates import Candidate

# Fixed identity and date make snapshot commits reproducible across runs and machines.
_SNAPSHOT_ENV = {
    "GIT_AUTHOR_NAME": "hindsight",
    "GIT_AUTHOR_EMAIL": "hindsight@localhost",
    "GIT_COMMITTER_NAME": "hindsight",
    "GIT_COMMITTER_EMAIL": "hindsight@localhost",
    "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
    "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
}


class SnapshotError(RuntimeError):
    """Raised when a snapshot cannot be built; the message becomes the discard reason."""


@dataclass(frozen=True)
class Snapshot:
    workdir: Path  # what the agent receives
    gold_dir: Path  # gold test files, mirrored at their repository paths
    reference_patch: Path  # the commit's non-test changes, for the positive control
    gold_test_files: tuple[str, ...]
    stripped_files: tuple[str, ...]


def _run(*args: str, cwd: Path | None = None, env: dict[str, str] | None = None) -> bytes:
    try:
        return subprocess.run(list(args), cwd=cwd, env=env, capture_output=True, check=True).stdout
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode(errors="replace").strip().splitlines()
        raise SnapshotError(
            f"{' '.join(args[:3])} failed: {detail[-1] if detail else exc}"
        ) from exc


def _extract(archive: bytes, dest: Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        try:
            tar.extractall(dest, filter="data")
        except TypeError:  # Python < 3.11.4 has no extraction filters
            for member in tar.getmembers():
                target = (dest / member.name).resolve()
                if not target.is_relative_to(dest.resolve()):
                    raise SnapshotError(f"unsafe path in archive: {member.name}") from None
            tar.extractall(dest)


def _strip(workdir: Path, names: Iterable[str]) -> tuple[str, ...]:
    """Remove instruction files (e.g. CLAUDE.md) at any depth; return their relative paths.

    Symlinks count too: in sqlglot, CLAUDE.md is a symlink to AGENTS.md, and removing the
    target first would leave a dangling link that `is_file()` no longer reports.
    """
    wanted = set(names)
    removed = []
    for path in sorted(workdir.rglob("*")):
        if path.name in wanted and (path.is_symlink() or path.is_file()):
            removed.append(path.relative_to(workdir).as_posix())
            path.unlink()
    return tuple(removed)


def export_tree(repo: Path, ref: str, dest: Path) -> str:
    """Extract the tree of `ref` into `dest` (replacing it); return the tree id."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    tree = _run("git", "-C", str(repo), "rev-parse", f"{ref}^{{tree}}").decode().strip()
    _extract(_run("git", "-C", str(repo), "archive", ref), dest)
    return tree


def build_snapshot(
    repo: Path,
    candidate: Candidate,
    dest: Path,
    strip_files: Iterable[str] = (),
) -> Snapshot:
    """Build the task directory.

    - `work/`: parent tree in a fresh one-commit repository (what the agent receives);
    - `gold/`: the commit's test files (evaluator only);
    - `reference/reference.patch`: the commit's non-test changes (positive control only).
    """
    strip_files = tuple(strip_files)
    if dest.exists():
        try:
            shutil.rmtree(dest)
        except OSError as exc:
            raise SnapshotError(
                f"cannot replace the previous snapshot ({exc.strerror}: {exc.filename}); "
                "a leftover Docker container may still be using it"
            ) from exc
    workdir, gold_dir, ref_dir = dest / "work", dest / "gold", dest / "reference"
    workdir.mkdir(parents=True)
    gold_dir.mkdir()
    ref_dir.mkdir()

    _extract(_run("git", "-C", str(repo), "archive", candidate.parent), workdir)
    stripped = _strip(workdir, strip_files)

    env = {**os.environ, **_SNAPSHOT_ENV}
    _run("git", "init", "-q", "-b", "main", cwd=workdir, env=env)
    _run("git", "add", "-A", cwd=workdir, env=env)
    _run("git", "commit", "-q", "--allow-empty", "-m", "initial state", cwd=workdir, env=env)

    gold = []
    for test_file in candidate.test_files:
        try:
            content = _run("git", "-C", str(repo), "show", f"{candidate.sha}:{test_file}")
        except SnapshotError:
            continue  # the commit deleted this test file; nothing to restore
        target = gold_dir / test_file
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        gold.append(test_file)
    if not gold:
        raise SnapshotError("commit only deletes test files; no gold tests to restore")

    # Changes to stripped instruction files cannot apply to the snapshot, so leave them out.
    patch_files = [f for f in candidate.source_files if Path(f).name not in strip_files]
    reference_patch = ref_dir / "reference.patch"
    reference_patch.write_bytes(
        _run(
            "git",
            "-C",
            str(repo),
            "diff",
            "--binary",
            candidate.parent,
            candidate.sha,
            "--",
            *patch_files,
        )
        if patch_files
        else b""
    )
    return Snapshot(workdir, gold_dir, reference_patch, tuple(gold), stripped)
