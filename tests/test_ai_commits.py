import pytest

from hindsight.config import DEFAULT_AI_PATTERNS
from hindsight.mining.ai_commits import is_ai_assisted


@pytest.mark.parametrize(
    "message",
    [
        "fix(sqlite): parse REINDEX as a command [CLAUDE] (#8929)",
        "fix: preserve left joins [CODEX] (#8859)",
        "fix: thing\n\nCo-authored-by: Claude <noreply@anthropic.com>",
        "feat: x\n\nco-authored-by: GitHub Copilot <copilot@github.com>",
    ],
)
def test_detects_ai_assisted(message: str) -> None:
    assert is_ai_assisted(message, DEFAULT_AI_PATTERNS)


@pytest.mark.parametrize(
    "message",
    [
        "fix(jsonpath): raise ParseError for non-decimal array indexes (#8869)",
        "docs: mention Claude in the README",
        "fix: x\n\nCo-authored-by: inchang-ing <197932532+inchang-ing@users.noreply.github.com>",
    ],
)
def test_ignores_human_commits(message: str) -> None:
    assert not is_ai_assisted(message, DEFAULT_AI_PATTERNS)
