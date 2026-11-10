"""Fail-to-pass classification (RF-03, RF-04).

For each task, the gold tests run twice in throwaway containers built from the task image:

- **before**: the parent commit plus the gold tests (once);
- **after**: the same, plus the commit's reference patch (`stability_runs` times).

A test is fail-to-pass (F2P) when it fails or is missing before (e.g. the test module could not
even import the new code) and passes in every after run. It is pass-to-pass (P2P) when it
passes before and in every after run. Tests whose after outcome changes between runs are flaky
and are never used. A task is kept only if it has at least one F2P test.

Containers run without network and receive the gold tests and the reference patch through
read-only mounts; results come back with `docker cp`, so no root-owned files land on the host.
"""

from __future__ import annotations

import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from hindsight.mining.environment import Runner

PATCH_FAILED_EXIT = 90
NO_JUNIT_EXIT = 91
TIMEOUT_EXIT = 124

_IN_CONTAINER_SCRIPT = """
set -u
cd /work
if [ "$HINDSIGHT_APPLY_REFERENCE" = 1 ] && [ -s /hindsight/reference/reference.patch ]; then
  git apply --binary /hindsight/reference/reference.patch || exit 90
fi
cp -a /hindsight/gold/. /work/
mkdir -p /out
bash -c "$HINDSIGHT_TEST_COMMAND" > /out/test-output.txt 2>&1 || true
test -s /out/junit.xml || exit 91
"""


class Status(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


class GoldTestError(RuntimeError):
    """Raised when a test run produced no usable result; the message is the discard reason."""


def parse_junit(xml_text: str) -> dict[str, Status]:
    """Map `classname::name` to a status. Errors count as failures."""
    root = ET.fromstring(xml_text)
    results: dict[str, Status] = {}
    for case in root.iter("testcase"):
        test_id = f"{case.get('classname', '')}::{case.get('name', '')}"
        if case.find("failure") is not None or case.find("error") is not None:
            results[test_id] = Status.FAILED
        elif case.find("skipped") is not None:
            results.setdefault(test_id, Status.SKIPPED)
        else:
            results.setdefault(test_id, Status.PASSED)
    return results


@dataclass(frozen=True)
class Classification:
    f2p: tuple[str, ...]
    p2p: tuple[str, ...]
    flaky: tuple[str, ...]

    @property
    def accepted(self) -> bool:
        return bool(self.f2p)

    def discard_reason(self) -> str | None:
        if self.accepted:
            return None
        if self.flaky:
            return "no stable fail-to-pass test (flaky tests found)"
        return "no fail-to-pass test"


def classify(before: dict[str, Status], after_runs: list[dict[str, Status]]) -> Classification:
    if not after_runs:
        raise ValueError("at least one after run is required")
    all_ids = set().union(*after_runs)
    flaky = sorted(t for t in all_ids if len({run.get(t) for run in after_runs}) > 1)
    stable_pass = sorted(
        t for t in all_ids if t not in flaky and after_runs[0].get(t) == Status.PASSED
    )
    f2p = [t for t in stable_pass if before.get(t, Status.FAILED) == Status.FAILED]
    p2p = [t for t in stable_pass if before.get(t) == Status.PASSED]
    return Classification(tuple(f2p), tuple(p2p), tuple(flaky))


class GoldTestRunner:
    def __init__(self, runner: Runner, test_command: str, timeout_s: float = 900) -> None:
        self._run = runner
        self._command = test_command
        self._timeout = timeout_s

    def run(
        self, image: str, task_dir: Path, apply_reference: bool, label: str
    ) -> dict[str, Status]:
        """Run the gold tests once; save the JUnit file as `task_dir/junit/<label>.xml`."""
        name = f"hindsight-{label}-{uuid.uuid4().hex[:10]}"
        out_dir = task_dir / "junit"
        out_dir.mkdir(exist_ok=True)
        try:
            result = self._run(
                [
                    "docker",
                    "run",
                    "--name",
                    name,
                    "--network",
                    "none",
                    "-v",
                    f"{(task_dir / 'gold').resolve()}:/hindsight/gold:ro",
                    "-v",
                    f"{(task_dir / 'reference').resolve()}:/hindsight/reference:ro",
                    "-e",
                    f"HINDSIGHT_APPLY_REFERENCE={1 if apply_reference else 0}",
                    "-e",
                    f"HINDSIGHT_TEST_COMMAND={self._command}",
                    image,
                    "bash",
                    "-c",
                    _IN_CONTAINER_SCRIPT,
                ],
                self._timeout,
            )
            if result.returncode == TIMEOUT_EXIT:
                raise GoldTestError(f"test run timed out after {self._timeout:.0f}s")
            if result.returncode == PATCH_FAILED_EXIT:
                raise GoldTestError("reference patch did not apply")
            if result.returncode == NO_JUNIT_EXIT:
                self._copy(name, "/out/test-output.txt", out_dir / f"{label}.txt")
                raise GoldTestError("test command produced no JUnit XML at /out/junit.xml")
            if result.returncode != 0:
                raise GoldTestError(f"test container failed with exit code {result.returncode}")
            junit = out_dir / f"{label}.xml"
            self._copy(name, "/out/junit.xml", junit)
            self._copy(name, "/out/test-output.txt", out_dir / f"{label}.txt")
            try:
                return parse_junit(junit.read_text())
            except (ET.ParseError, OSError) as exc:
                raise GoldTestError(f"unreadable JUnit XML: {exc}") from exc
        finally:
            self._run(["docker", "rm", "-f", name], 60)

    def _copy(self, container: str, source: str, dest: Path) -> None:
        self._run(["docker", "cp", f"{container}:{source}", str(dest)], 60)

    def classify_task(self, image: str, task_dir: Path, stability_runs: int = 2) -> Classification:
        before = self.run(image, task_dir, apply_reference=False, label="before")
        after = [
            self.run(image, task_dir, apply_reference=True, label=f"after-{i + 1}")
            for i in range(stability_runs)
        ]
        return classify(before, after)
