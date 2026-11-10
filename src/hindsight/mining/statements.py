"""Task statements (RF-07, RF-08) and leakage hints (RF-10).

The statement is what the agent receives. It comes from the first source in the cascade that
yields usable text:

1. ``issue``: the GitHub issue or pull request referenced in the commit subject, e.g. "(#8869)";
2. ``commit``: the full commit message, if it is long enough to describe the problem;
3. ``llm_from_tests``: text written by a model that sees **only the gold test diff and the
   fail-to-pass test names**, never the source changes, through a user-configured command.

Every statement is reviewed by a human before use (`hindsight review`), helped by the leakage
hints computed here.
"""

from __future__ import annotations

import json
import keyword
import os
import re
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

ISSUE = "issue"
COMMIT = "commit"
LLM = "llm_from_tests"
ALL_SOURCES = (ISSUE, COMMIT, LLM)

_REF_IN_SUBJECT = re.compile(r"\(#(\d+)\)\s*$|#(\d+)\b")
_CLOSING_REF = re.compile(r"(?i)\b(?:fix(?:es|ed)?|close[sd]?|resolve[sd]?)\s+#(\d+)\b")
_SOLUTION_LANGUAGE = re.compile(
    r"(?i)\b(the fix|this (?:pr|pull request|change|patch|commit)|the patch|verification)\b"
)
_GITHUB_REMOTE = re.compile(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,}")

LLM_PROMPT = """You write task statements for a coding agent that must change a repository.
Below are the tests that the change must make pass, and the names of the tests that currently
fail. Describe, in a few sentences and in English, the behavior the user expects: WHAT must
change, as a user or maintainer would report it. Never explain HOW to implement it, never name
functions, files or code that the fix should add or edit, and never mention the tests themselves.
Output only the statement.

Failing tests that must pass:
{f2p}

Test changes:
{test_diff}
"""

HttpGet = Callable[[str, dict[str, str]], tuple[int, str]]
CommandRunner = Callable[[Sequence[str], str, float], tuple[int, str]]


@dataclass(frozen=True)
class Statement:
    text: str
    source: str
    ai_disclosed: bool = False  # the issue/PR text says it was produced with AI help


def urllib_get(url: str, headers: dict[str, str]) -> tuple[int, str]:
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except (urllib.error.URLError, TimeoutError):
        return 0, ""


def shell_runner(command: Sequence[str], stdin: str, timeout: float) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            list(command), input=stdin, capture_output=True, text=True, timeout=timeout
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return 1, ""
    return proc.returncode, proc.stdout


def referenced_number(subject: str) -> int | None:
    match = _REF_IN_SUBJECT.search(subject)
    if not match:
        return None
    return int(match.group(1) or match.group(2))


def github_repo_from_remote(repo: Path) -> str | None:
    try:
        url = subprocess.run(
            ["git", "-C", str(repo), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except subprocess.CalledProcessError:
        return None
    match = _GITHUB_REMOTE.search(url)
    return f"{match.group(1)}/{match.group(2)}" if match else None


class StatementWriter:
    def __init__(
        self,
        sources: Sequence[str] = ALL_SOURCES,
        ai_patterns: Sequence[str] = (),
        github_repo: str | None = None,
        min_commit_chars: int = 40,
        llm_command: str | None = None,
        http_get: HttpGet = urllib_get,
        run_command: CommandRunner = shell_runner,
        llm_timeout_s: float = 180,
    ) -> None:
        unknown = set(sources) - set(ALL_SOURCES)
        if unknown:
            raise ValueError(f"unknown statement sources: {sorted(unknown)}")
        self._sources = tuple(sources)
        self._ai_patterns = tuple(ai_patterns)
        self._github_repo = github_repo
        self._min_commit_chars = min_commit_chars
        self._llm_command = llm_command
        self._http_get = http_get
        self._run = run_command
        self._llm_timeout = llm_timeout_s

    def write(
        self, subject: str, message: str, f2p_tests: Sequence[str], test_diff: str
    ) -> Statement | None:
        for source in self._sources:
            disclosed = False
            if source == ISSUE:
                text, disclosed = self._from_issue(subject)
            elif source == COMMIT:
                text = self._from_commit(message)
            else:
                text = self._from_llm(f2p_tests, test_diff)
            if disclosed:
                return Statement(text or "", source, ai_disclosed=True)
            if text:
                return Statement(text, source)
        return None

    def _fetch(self, number: int) -> str | None:
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "hindsight-evals"}
        if token := os.environ.get("GITHUB_TOKEN"):
            headers["Authorization"] = f"Bearer {token}"
        url = f"https://api.github.com/repos/{self._github_repo}/issues/{number}"
        status, body = self._http_get(url, headers)
        if status != 200:
            return None
        data = json.loads(body)
        title = (data.get("title") or "").strip()
        text = (data.get("body") or "").strip()
        combined = f"{title}\n\n{text}".strip() if text else title
        return combined or None

    def _from_issue(self, subject: str) -> tuple[str | None, bool]:
        """Prefer the issue a PR closes ("Fixes #8955"): it describes the problem, not the fix."""
        number = referenced_number(subject)
        if number is None or not self._github_repo:
            return None, False
        referenced = self._fetch(number)
        if referenced is None:
            return None, False
        disclosed = any(re.search(p, referenced) for p in self._ai_patterns)
        closing = _CLOSING_REF.search(referenced)
        if closing and int(closing.group(1)) != number:
            original = self._fetch(int(closing.group(1)))
            if original:
                return original, disclosed
        return referenced, disclosed

    def _from_commit(self, message: str) -> str | None:
        text = message.strip()
        return text if len(text) >= self._min_commit_chars else None

    def _from_llm(self, f2p_tests: Sequence[str], test_diff: str) -> str | None:
        if not self._llm_command:
            return None
        prompt = LLM_PROMPT.format(f2p="\n".join(f2p_tests), test_diff=test_diff)
        code, output = self._run(["bash", "-c", self._llm_command], prompt, self._llm_timeout)
        text = output.strip()
        return text if code == 0 and text else None


def _code_like(name: str, context: str) -> bool:
    """Keep names that read as code (snake_case, CamelCase, calls), not plain words."""
    if keyword.iskeyword(name):
        return False
    return (
        "_" in name
        or re.search(r"[a-z][A-Z]", name) is not None
        or re.search(rf"\b{re.escape(name)}\(", context) is not None
    )


def leakage_hints(statement: str, reference_patch: str, gold_tests_text: str) -> list[str]:
    """Flag parts of the statement that look like the solution rather than the problem.

    - code names from the reference patch (added, removed or surrounding lines, hunk headers)
      that the tests never use: they point at *where* or *how* to change the code;
    - code fragments copied from added lines of the patch;
    - wording that describes the change itself ("the fix", "this PR").
    Names the tests use are fine: they describe *what* is expected.
    """
    lines = reference_patch.splitlines()
    added = [line[1:] for line in lines if line.startswith("+") and not line.startswith("+++")]
    body = "\n".join(
        line for line in lines if not line.startswith(("+++", "---", "diff ", "index "))
    )
    patch_ids = {name for name in _IDENTIFIER.findall(body) if _code_like(name, body)}
    test_ids = set(_IDENTIFIER.findall(gold_tests_text))
    hints = [
        f"mentions '{name}', code the fix touches but the tests never use"
        for name in sorted(patch_ids - test_ids)
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", statement)
    ]
    for line in added:
        fragment = line.strip()
        if len(fragment) >= 15 and fragment in statement:
            hints.append(f"copies code from the fix: '{fragment[:60]}'")
    phrases = sorted({m.group(1).lower() for m in _SOLUTION_LANGUAGE.finditer(statement)})
    if phrases:
        hints.append("describes the change itself: " + ", ".join(f"'{p}'" for p in phrases))
    return hints
