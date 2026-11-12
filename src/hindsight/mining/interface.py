"""What the gold tests need beyond the statement (helps reviewers write fair statements).

- `required_new_names`: code names that the tests add but that do not exist anywhere in the
  parent commit's source, e.g. `exp.OptionsProperty`. An agent cannot guess them, so the
  statement must mention them; otherwise the task is ambiguous rather than hard.
- `gold_test_sources`: the source of each fail-to-pass test function, for `review`'s
  "[t]ests" option.
"""

from __future__ import annotations

import difflib
import keyword
import re
from collections.abc import Iterable
from pathlib import Path

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,}")
_STRING = re.compile(r"""(?:[rbfuRBFU]{0,2})('(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*")""")
_SOURCE_SUFFIXES = {".py", ".pyi"}
# Test-framework API (unittest, pytest) is never something the change must provide.
_FRAMEWORK = re.compile(r"^(assert|fail|subTest|setUp|tearDown|addCleanup|skipTest|mock_|patch)")


def _code_like(name: str) -> bool:
    if keyword.iskeyword(name) or name.startswith("test") or _FRAMEWORK.match(name):
        return False
    return "_" in name.strip("_") or re.search(r"[a-z][A-Z]|^[A-Z][a-z]+[A-Z]", name) is not None


def _added_lines(old: str, new: str) -> list[str]:
    diff = difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0)
    return [line[1:] for line in diff if line.startswith("+") and not line.startswith("+++")]


def _identifiers_outside_strings(lines: Iterable[str]) -> set[str]:
    names: set[str] = set()
    for line in lines:
        code = _STRING.sub(" ", line.split("#", 1)[0])
        names.update(name for name in _IDENTIFIER.findall(code) if _code_like(name))
    return names


def _existing_identifiers(workdir: Path) -> set[str]:
    """Every name in the parent's Python files, test helpers included (e.g. validate_all)."""
    names: set[str] = set()
    for path in workdir.rglob("*"):
        if path.suffix not in _SOURCE_SUFFIXES or not path.is_file() or ".git" in path.parts:
            continue
        names.update(_IDENTIFIER.findall(path.read_text(errors="replace")))
    return names


def required_new_names(
    task_dir: Path, gold_files: Iterable[str], test_paths: Iterable[str]
) -> list[str]:
    """Names added by the gold tests (outside string literals) that the parent code lacks."""
    added: list[str] = []
    for name in gold_files:
        gold = task_dir / "gold" / name
        if not gold.exists():
            continue
        before = task_dir / "work" / name
        old = before.read_text(errors="replace") if before.exists() else ""
        added += _added_lines(old, gold.read_text(errors="replace"))
    candidates = _identifiers_outside_strings(added)
    if not candidates:
        return []
    return sorted(candidates - _existing_identifiers(task_dir / "work"))


def gold_test_sources(task_dir: Path, f2p_tests: Iterable[str], gold_files: Iterable[str]) -> str:
    """Source of each fail-to-pass test function found in the gold files."""
    texts = {
        name: (task_dir / "gold" / name).read_text(errors="replace")
        for name in gold_files
        if (task_dir / "gold" / name).exists()
    }
    blocks: list[str] = []
    for test_id in f2p_tests:
        function = test_id.rsplit("::", 1)[-1].split("[", 1)[0]
        for name, text in texts.items():
            block = _function_block(text, function)
            if block:
                blocks.append(f"# {name}\n{block}")
                break
        else:
            blocks.append(f"# {test_id}: source not found in the gold test files")
    if not blocks:
        return "(no fail-to-pass tests)"
    return "\n\n".join(blocks)


def _function_block(text: str, function: str) -> str | None:
    lines = text.splitlines()
    pattern = re.compile(rf"^(\s*)(?:async\s+)?def {re.escape(function)}\(")
    for start, line in enumerate(lines):
        match = pattern.match(line)
        if not match:
            continue
        indent = len(match.group(1))
        end = start + 1
        while end < len(lines):
            current = lines[end]
            stripped = current.strip()
            if stripped and len(current) - len(current.lstrip()) <= indent:
                if not stripped.startswith(("#", ")", "]", "}")):
                    break
            end += 1
        return "\n".join(lines[start:end]).rstrip()
    return None
