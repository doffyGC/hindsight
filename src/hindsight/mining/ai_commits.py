"""Detection of AI-assisted commits (RF-24)."""

from __future__ import annotations

import re
from collections.abc import Iterable


def is_ai_assisted(message: str, patterns: Iterable[str]) -> bool:
    """Return True when the full commit message matches any AI-authorship pattern.

    Patterns cover subject tags such as "[CLAUDE]" and Co-authored-by trailers.
    """
    return any(re.search(pattern, message) for pattern in patterns)
