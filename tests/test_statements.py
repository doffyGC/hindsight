import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from hindsight.mining.statements import (
    StatementWriter,
    github_repo_from_remote,
    leakage_hints,
    referenced_number,
)


@pytest.mark.parametrize(
    ("subject", "number"),
    [
        ("fix(jsonpath): raise ParseError for non-decimal array indexes (#8869)", 8869),
        ("Fix #123: crash on empty input", 123),
        ("refactor: tidy up", None),
    ],
)
def test_referenced_number(subject: str, number: int | None) -> None:
    assert referenced_number(subject) == number


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/tobymao/sqlglot.git",
        "git@github.com:tobymao/sqlglot.git",
        "https://github.com/tobymao/sqlglot",
    ],
)
def test_github_repo_from_remote(repo: Path, url: str) -> None:
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", url], check=True)
    assert github_repo_from_remote(repo) == "tobymao/sqlglot"


def test_no_remote_means_no_github_repo(repo: Path) -> None:
    assert github_repo_from_remote(repo) is None


class FakeHttp:
    def __init__(self, status: int, payload: dict | None = None):
        self.status, self.payload, self.urls = status, payload, []

    def __call__(self, url: str, headers: dict[str, str]) -> tuple[int, str]:
        self.urls.append(url)
        return self.status, json.dumps(self.payload or {})


class FakeLlm:
    def __init__(self, output: str = "Indexes in exponent form must be rejected.", code: int = 0):
        self.output, self.code, self.prompts = output, code, []

    def __call__(self, command: Sequence[str], stdin: str, timeout: float) -> tuple[int, str]:
        self.prompts.append(stdin)
        return self.code, self.output


LONG_MESSAGE = "fix: crash\n\nParsing '$[2E]' crashes with ValueError instead of a ParseError."


def test_issue_is_preferred() -> None:
    http = FakeHttp(200, {"title": "Crash on $[2E]", "body": "It raises ValueError."})
    writer = StatementWriter(github_repo="o/r", http_get=http)
    statement = writer.write("fix: crash (#42)", LONG_MESSAGE, [], "")
    assert statement.source == "issue"
    assert statement.text == "Crash on $[2E]\n\nIt raises ValueError."
    assert http.urls == ["https://api.github.com/repos/o/r/issues/42"]


def test_falls_back_to_commit_when_issue_is_missing() -> None:
    writer = StatementWriter(github_repo="o/r", http_get=FakeHttp(404))
    statement = writer.write("fix: crash (#42)", LONG_MESSAGE, [], "")
    assert statement.source == "commit"
    assert "ParseError" in statement.text


def test_short_commit_message_falls_back_to_llm_with_tests_only() -> None:
    llm = FakeLlm()
    writer = StatementWriter(github_repo=None, llm_command="claude -p", run_command=llm)
    statement = writer.write("fix", "fix", ["t::test_new"], "+def test_new(): ...")
    assert statement.source == "llm_from_tests"
    assert "t::test_new" in llm.prompts[0] and "+def test_new(): ..." in llm.prompts[0]


def test_no_usable_source_returns_none() -> None:
    writer = StatementWriter(github_repo=None, run_command=FakeLlm(code=1), llm_command="x")
    assert writer.write("fix", "fix", [], "") is None


def test_sources_order_is_respected() -> None:
    writer = StatementWriter(sources=["commit"], github_repo="o/r", http_get=FakeHttp(200))
    assert writer.write("fix (#1)", LONG_MESSAGE, [], "").source == "commit"


def test_unknown_source_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown statement sources"):
        StatementWriter(sources=["jira"])


PATCH = """--- a/pkg/jsonpath.py
+++ b/pkg/jsonpath.py
-            return int(number)
+            try:
+                return int(number)
+            except ValueError:
+                raise ParseError(_error(f"Invalid array index '{number}'"))
"""
GOLD = "from pkg.errors import ParseError\n\ndef test_x():\n    with raises(ParseError): ...\n"


def test_leakage_hints_flag_solution_details() -> None:
    leaky = "Wrap the conversion and raise ParseError(_error(...)) from the ValueError."
    hints = leakage_hints(leaky, PATCH, GOLD)
    assert any("'_error'" in h for h in hints)
    assert any("'ValueError'" in h for h in hints)
    assert not any("'ParseError'" in h for h in hints)  # the tests use it: that is the *what*


def test_leakage_hints_flag_copied_code() -> None:
    hints = leakage_hints('Add except ValueError: around "return int(number)".', PATCH, GOLD)
    assert any("copies code from the fix" in h for h in hints)


def test_clean_statement_has_no_hints() -> None:
    clean = "Indexes in exponent form like $[2E] must be reported as a ParseError."
    assert leakage_hints(clean, PATCH, GOLD) == []


class FakeGitHub:
    def __init__(self, items: dict[int, dict]):
        self.items, self.urls = items, []

    def __call__(self, url: str, headers: dict[str, str]) -> tuple[int, str]:
        self.urls.append(url)
        number = int(url.rsplit("/", 1)[1])
        if number not in self.items:
            return 404, ""
        return 200, json.dumps(self.items[number])


def test_pr_is_followed_to_the_issue_it_closes() -> None:
    github = FakeGitHub(
        {
            8959: {"title": "fix(duckdb): ...", "body": "Fixes #8955.\n\nThe fix parses ..."},
            8955: {"title": "Nested INITCAP fails", "body": "INITCAP(INITCAP(x)) raises."},
        }
    )
    writer = StatementWriter(github_repo="o/r", http_get=github)
    statement = writer.write("fix(duckdb): parse INITCAP (#8959)", "", [], "")
    assert statement.text == "Nested INITCAP fails\n\nINITCAP(INITCAP(x)) raises."
    assert statement.source == "issue" and not statement.ai_disclosed


def test_ai_disclosure_in_pr_is_reported() -> None:
    from hindsight.config import DEFAULT_AI_PATTERNS

    body = "Fixes #8955.\n\nThis PR was produced with AI assistance (WorkBuddy)."
    github = FakeGitHub({8959: {"title": "fix", "body": body}, 8955: {"title": "bug"}})
    writer = StatementWriter(github_repo="o/r", http_get=github, ai_patterns=DEFAULT_AI_PATTERNS)
    assert writer.write("fix (#8959)", "", [], "").ai_disclosed


INITCAP_PATCH = """--- a/sqlglot/dialects/duckdb.py
+++ b/sqlglot/dialects/duckdb.py
@@ -120,6 +120,6 @@ def _build_capitalization_sql(self, expression):
     value_to_split = self.sql(expression, "this")
-    case = exp.case().when(cond, maybe_parse(value_to_split))
+    case = exp.case().when(cond, maybe_parse(value_to_split, dialect="duckdb"))
"""
INITCAP_TESTS = "def test_initcap(self):\n    self.validate_all('INITCAP(INITCAP(x))')\n"


def test_hints_flag_existing_code_names_and_solution_language() -> None:
    statement = (
        "Nested INITCAP raises ParseError. `_build_capitalization_sql` passes strings to "
        "`maybe_parse` with the default dialect. The fix parses them with dialect='duckdb'."
    )
    hints = leakage_hints(statement, INITCAP_PATCH, INITCAP_TESTS)
    assert any("'_build_capitalization_sql'" in h for h in hints)
    assert any("'maybe_parse'" in h for h in hints)
    assert any("'the fix'" in h for h in hints)


def test_problem_only_statement_has_no_hints() -> None:
    statement = "Generating DuckDB SQL for nested INITCAP, e.g. INITCAP(INITCAP(x)), fails."
    assert leakage_hints(statement, INITCAP_PATCH, INITCAP_TESTS) == []


def test_hints_flag_reporters_guessing_the_cause() -> None:
    statement = (
        "Typed integer division drops the NULLIF guard. I think the cause is in the generator; "
        "using the right operand there fixes it."
    )
    hints = leakage_hints(statement, "", "")
    assert any("'the cause'" in h and "'fixes it'" in h for h in hints)


def test_issue_links_are_followed_like_fixes_keywords() -> None:
    body = "Summary\n\nFix for issue: https://github.com/o/r/issues/8916\n\n## Changes\n- add X"
    github = FakeGitHub(
        {
            8917: {"title": "feat: ignore grants", "body": body},
            8916: {"title": "IGNORE GRANTS fails"},
        }
    )
    writer = StatementWriter(github_repo="o/r", http_get=github)
    assert writer.write("feat(snowflake): support ignore grants (#8917)", "", [], "").text == (
        "IGNORE GRANTS fails"
    )


def test_hint_for_commit_style_title() -> None:
    hints = leakage_hints("fix(optimizer): preserve subqueries\n\nThey disappear.", "", "")
    assert hints == ["starts with a commit-style title, which usually describes the change"]


def test_names_that_tests_contain_are_not_flagged() -> None:
    patch = "@@ -1 +1 @@ class Teradata(Dialect):\n-    x = 1\n+    unnest_subqueries = 2\n"
    tests = "tests/fixtures/optimizer/unnest_subqueries.sql\nvalidate(dialect='teradata')"
    statement = "Teradata's unnest_subqueries rule hoists subqueries."
    assert leakage_hints(statement, patch, tests) == []
