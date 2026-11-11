"""SQLite store for mining results (spec AR-02: tables `tasks` and `mining_log`).

Every candidate leaves a trail in `mining_log`: one row per funnel stage it reached, saying
whether it passed or was discarded, and why. Tasks that survive are kept in `tasks`.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS mining_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    commit_sha  TEXT NOT NULL,
    stage       TEXT NOT NULL,
    outcome     TEXT NOT NULL CHECK (outcome IN ('passed', 'discarded')),
    reason      TEXT,
    created_at  TEXT NOT NULL,
    UNIQUE (commit_sha, stage)
);
CREATE TABLE IF NOT EXISTS tasks (
    commit_sha        TEXT PRIMARY KEY,
    parent_sha        TEXT NOT NULL,
    commit_date       TEXT NOT NULL,
    subject           TEXT NOT NULL,
    statement         TEXT,
    statement_source  TEXT,
    gold_test_files   TEXT NOT NULL DEFAULT '[]',
    f2p_tests         TEXT NOT NULL DEFAULT '[]',
    p2p_tests         TEXT NOT NULL DEFAULT '[]',
    snapshot_path     TEXT,
    image_digest      TEXT,
    curation_status   TEXT NOT NULL DEFAULT 'pending'
                      CHECK (curation_status IN ('pending', 'approved', 'discarded')),
    curated_by        TEXT,
    curated_at        TEXT,
    created_at        TEXT NOT NULL
);
"""


class Stage(StrEnum):
    """Funnel stages, in pipeline order (RF-06)."""

    CANDIDATE = "candidate"
    AI_FILTER = "ai_filter"
    SNAPSHOT = "snapshot"
    BUILD = "build"
    FAIL_TO_PASS = "fail_to_pass"
    REVIEW = "review"


class Outcome(StrEnum):
    PASSED = "passed"
    DISCARDED = "discarded"


@dataclass
class Task:
    commit_sha: str
    parent_sha: str
    commit_date: str
    subject: str
    gold_test_files: list[str] = field(default_factory=list)
    snapshot_path: str | None = None
    statement: str | None = None
    statement_source: str | None = None
    f2p_tests: list[str] = field(default_factory=list)
    p2p_tests: list[str] = field(default_factory=list)
    image_digest: str | None = None
    curation_status: str = "pending"


@dataclass(frozen=True)
class FunnelRow:
    stage: Stage
    passed: int
    discarded: int
    reasons: dict[str, int]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Store:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._db = connection

    @classmethod
    @contextmanager
    def open(cls, path: Path) -> Iterator[Store]:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path)
        try:
            connection.executescript(_SCHEMA)
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif version != SCHEMA_VERSION:
                raise RuntimeError(f"{path} has schema v{version}; expected v{SCHEMA_VERSION}")
            yield cls(connection)
            connection.commit()
        finally:
            connection.close()

    def commit(self) -> None:
        """Persist pending changes now (used by interactive commands after each decision)."""
        self._db.commit()

    # mining_log ------------------------------------------------------------------------

    def log(self, commit_sha: str, stage: Stage, outcome: Outcome, reason: str | None = None):
        """Record (or overwrite, when re-mining) the outcome of one stage for one commit."""
        self._db.execute(
            """INSERT INTO mining_log (commit_sha, stage, outcome, reason, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT (commit_sha, stage)
               DO UPDATE SET outcome = excluded.outcome, reason = excluded.reason,
                             created_at = excluded.created_at""",
            (commit_sha, str(stage), str(outcome), reason, _now()),
        )

    def outcome(self, commit_sha: str, stage: Stage) -> Outcome | None:
        row = self._db.execute(
            "SELECT outcome FROM mining_log WHERE commit_sha = ? AND stage = ?",
            (commit_sha, str(stage)),
        ).fetchone()
        return Outcome(row[0]) if row else None

    def funnel(self) -> list[FunnelRow]:
        rows = []
        for stage in Stage:
            counts = dict(
                self._db.execute(
                    "SELECT outcome, COUNT(*) FROM mining_log WHERE stage = ? GROUP BY outcome",
                    (str(stage),),
                ).fetchall()
            )
            reasons = dict(
                self._db.execute(
                    """SELECT COALESCE(reason, 'unspecified'), COUNT(*) FROM mining_log
                       WHERE stage = ? AND outcome = 'discarded' GROUP BY 1 ORDER BY 2 DESC""",
                    (str(stage),),
                ).fetchall()
            )
            rows.append(
                FunnelRow(stage, counts.get("passed", 0), counts.get("discarded", 0), reasons)
            )
        return rows

    # tasks -----------------------------------------------------------------------------

    def upsert_task(self, task: Task) -> None:
        self._db.execute(
            """INSERT INTO tasks (commit_sha, parent_sha, commit_date, subject, statement,
                   statement_source, gold_test_files, f2p_tests, p2p_tests, snapshot_path,
                   image_digest, curation_status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (commit_sha) DO UPDATE SET
                   statement = excluded.statement,
                   statement_source = excluded.statement_source,
                   gold_test_files = excluded.gold_test_files,
                   f2p_tests = excluded.f2p_tests,
                   p2p_tests = excluded.p2p_tests,
                   snapshot_path = excluded.snapshot_path,
                   image_digest = excluded.image_digest""",
            (
                task.commit_sha,
                task.parent_sha,
                task.commit_date,
                task.subject,
                task.statement,
                task.statement_source,
                json.dumps(task.gold_test_files),
                json.dumps(task.f2p_tests),
                json.dumps(task.p2p_tests),
                task.snapshot_path,
                task.image_digest,
                task.curation_status,
                _now(),
            ),
        )

    def get_task(self, commit_sha: str) -> Task | None:
        row = self._db.execute(
            """SELECT commit_sha, parent_sha, commit_date, subject, gold_test_files,
                      snapshot_path, statement, statement_source, f2p_tests, p2p_tests,
                      image_digest, curation_status
               FROM tasks WHERE commit_sha = ?""",
            (commit_sha,),
        ).fetchone()
        if row is None:
            return None
        return Task(
            commit_sha=row[0],
            parent_sha=row[1],
            commit_date=row[2],
            subject=row[3],
            gold_test_files=json.loads(row[4]),
            snapshot_path=row[5],
            statement=row[6],
            statement_source=row[7],
            f2p_tests=json.loads(row[8]),
            p2p_tests=json.loads(row[9]),
            image_digest=row[10],
            curation_status=row[11],
        )

    def accepted_tasks(self, curation_status: str | None = None) -> list[Task]:
        """Tasks that passed fail-to-pass classification, oldest commit first."""
        query = """SELECT t.commit_sha FROM tasks t
                   JOIN mining_log m ON m.commit_sha = t.commit_sha
                   WHERE m.stage = ? AND m.outcome = 'passed'
                   AND NOT EXISTS (SELECT 1 FROM mining_log a WHERE a.commit_sha = t.commit_sha
                                   AND a.stage = ? AND a.outcome = 'discarded')"""
        params: list[str] = [str(Stage.FAIL_TO_PASS), str(Stage.AI_FILTER)]
        if curation_status is not None:
            query += " AND t.curation_status = ?"
            params.append(curation_status)
        query += " ORDER BY t.commit_date, t.commit_sha"
        shas = [row[0] for row in self._db.execute(query, params).fetchall()]
        return [task for sha in shas if (task := self.get_task(sha)) is not None]

    def set_statement(self, commit_sha: str, statement: str, source: str) -> None:
        self._db.execute(
            "UPDATE tasks SET statement = ?, statement_source = ? WHERE commit_sha = ?",
            (statement, source, commit_sha),
        )

    def set_curation(self, commit_sha: str, status: str, curated_by: str) -> None:
        """Record a review decision (RF-11) and mirror it in the funnel."""
        self._db.execute(
            "UPDATE tasks SET curation_status = ?, curated_by = ?, curated_at = ? "
            "WHERE commit_sha = ?",
            (status, curated_by, _now(), commit_sha),
        )
        if status == "approved":
            self.log(commit_sha, Stage.REVIEW, Outcome.PASSED)
        elif status == "discarded":
            self.log(commit_sha, Stage.REVIEW, Outcome.DISCARDED, "discarded in review")

    def curation_info(self, commit_sha: str) -> tuple[str | None, str | None]:
        row = self._db.execute(
            "SELECT curated_by, curated_at FROM tasks WHERE commit_sha = ?", (commit_sha,)
        ).fetchone()
        return (row[0], row[1]) if row else (None, None)

    def count_tasks(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
