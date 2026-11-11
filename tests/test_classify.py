import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from hindsight.mining.classify import (
    GoldTestError,
    GoldTestRunner,
    Status,
    classify,
    parse_junit,
)
from hindsight.mining.environment import CommandResult

P, F, S = Status.PASSED, Status.FAILED, Status.SKIPPED

PYTEST_JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="5">
  <testcase classname="tests.test_a.TestA" name="test_ok" time="0.01"/>
  <testcase classname="tests.test_a.TestA" name="test_fail"><failure message="boom"/></testcase>
  <testcase classname="tests.test_a.TestA" name="test_error"><error message="oops"/></testcase>
  <testcase classname="tests.test_a.TestA" name="test_skip"><skipped message="nope"/></testcase>
  <testcase classname="" name="tests.test_b"><error message="collection failure"/></testcase>
</testsuite></testsuites>"""


def test_parse_junit_statuses() -> None:
    assert parse_junit(PYTEST_JUNIT) == {
        "tests.test_a.TestA::test_ok": P,
        "tests.test_a.TestA::test_fail": F,
        "tests.test_a.TestA::test_error": F,
        "tests.test_a.TestA::test_skip": S,
        "::tests.test_b": F,
    }


def test_parse_junit_single_testsuite_root() -> None:
    xml = '<testsuite><testcase classname="c" name="n"/></testsuite>'
    assert parse_junit(xml) == {"c::n": P}


def test_classify_f2p_and_p2p() -> None:
    before = {"old": P, "new": F, "broken": F}
    after = [{"old": P, "new": P, "broken": F}] * 2
    result = classify(before, after)
    assert result.f2p == ("new",)
    assert result.p2p == ("old",)
    assert result.accepted and result.discard_reason() is None


def test_tests_missing_before_count_as_failing() -> None:
    # e.g. the test module could not import the new code on the parent commit
    result = classify({"::tests.test_b": F}, [{"tests.test_b::test_x": P}])
    assert result.f2p == ("tests.test_b::test_x",)


def test_flaky_tests_are_never_used() -> None:
    result = classify({"new": F, "old": P}, [{"new": P, "old": P}, {"new": F, "old": P}])
    assert result.flaky == ("new",)
    assert result.f2p == ()
    assert result.p2p == ("old",)
    assert result.discard_reason() == "no stable fail-to-pass test (flaky tests found)"


def test_regressions_and_skips_are_excluded() -> None:
    result = classify({"a": P, "b": S}, [{"a": F, "b": S}])
    assert (result.f2p, result.p2p) == ((), ())
    assert result.discard_reason() == "no fail-to-pass test"


class FakeTestDocker:
    """Simulates `docker run/cp/rm` for test runs, keyed by run label (before, after)."""

    def __init__(self, results: dict[str, tuple[int, str]]):
        self.results = results
        self.calls: list[list[str]] = []
        self._labels: dict[str, str] = {}
        self.images: set[str] = set()

    def __call__(self, args: Sequence[str], timeout: float | None) -> CommandResult:
        args = list(args)
        self.calls.append(args)
        if args[:2] == ["docker", "ps"]:
            return CommandResult(0, getattr(self, "orphans", ""))
        if args[:2] == ["docker", "version"]:
            return CommandResult(0, "27")
        if args[:3] == ["docker", "image", "inspect"]:
            return (
                CommandResult(0, "sha256:abc") if args[-1] in self.images else CommandResult(1, "")
            )
        if args[:2] == ["docker", "build"]:
            self.images.add(args[3])
            return CommandResult(0, "")
        if args[:2] == ["docker", "run"]:
            name = args[args.index("--name") + 1]
            label = "before" if "-before-" in name else "after"
            self._labels[name] = label
            return CommandResult(self.results[label][0], "")
        if args[:2] == ["docker", "cp"]:
            name, source = args[2].split(":", 1)
            content = self.results[self._labels[name]][1] if source.endswith(".xml") else "log"
            Path(args[3]).write_text(content)
            return CommandResult(0, "")
        if args[:2] == ["docker", "rm"]:
            return CommandResult(0, "")
        raise AssertionError(f"unexpected command {args}")

    def removed(self) -> int:
        return sum(1 for c in self.calls if c[:2] == ["docker", "rm"])


@pytest.fixture
def task_dir(tmp_path: Path) -> Path:
    for sub in ("gold", "reference"):
        (tmp_path / sub).mkdir()
    return tmp_path


def _runner(docker: FakeTestDocker) -> GoldTestRunner:
    return GoldTestRunner(docker, "pytest --junitxml=/out/junit.xml", timeout_s=60)


def test_runner_classifies_and_cleans_up(task_dir: Path) -> None:
    before = '<testsuite><testcase classname="t" name="new"><failure/></testcase></testsuite>'
    after = '<testsuite><testcase classname="t" name="new"/></testsuite>'
    docker = FakeTestDocker({"before": (0, before), "after": (0, after)})

    result = _runner(docker).classify_task("img", task_dir, stability_runs=2)

    assert result.f2p == ("t::new",)
    runs = [c for c in docker.calls if c[:2] == ["docker", "run"]]
    assert len(runs) == 3 and docker.removed() == 3
    assert all("--network" in r and r[r.index("--network") + 1] == "none" for r in runs)
    assert all(any(a.endswith(":/hindsight/gold:ro") for a in r) for r in runs)
    assert "HINDSIGHT_APPLY_REFERENCE=0" in runs[0] and "HINDSIGHT_APPLY_REFERENCE=1" in runs[1]
    assert sorted(p.name for p in (task_dir / "junit").glob("*.xml")) == [
        "after-1.xml",
        "after-2.xml",
        "before.xml",
    ]


@pytest.mark.parametrize(
    ("code", "message"),
    [
        (90, "reference patch did not apply"),
        (91, "no JUnit XML"),
        (124, "timed out after 60s"),
        (137, "exit code 137"),
    ],
)
def test_runner_failures_become_discard_reasons(task_dir: Path, code: int, message: str) -> None:
    docker = FakeTestDocker({"before": (code, ""), "after": (code, "")})
    with pytest.raises(GoldTestError, match=message):
        _runner(docker).run("img", task_dir, apply_reference=False, label="before")
    assert docker.removed() == 1


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "version"], capture_output=True).returncode == 0


@pytest.mark.skipif(not _docker_ready(), reason="needs a running Docker daemon")
def test_real_end_to_end_mining(tmp_path: Path) -> None:
    """A tiny repository with one buggy function and one fixing commit becomes one F2P task."""
    from typer.testing import CliRunner

    from hindsight.cli import app

    from .conftest import commit

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    commit(
        repo,
        {
            "calc.py": "def add(a, b):\n    return a - b\n",
            "tests/test_calc.py": (
                "from calc import add\n\ndef test_zero():\n    assert add(0, 0) == 0\n"
            ),
        },
        "init",
    )
    commit(
        repo,
        {
            "calc.py": "def add(a, b):\n    return a + b\n",
            "tests/test_calc.py": (
                "from calc import add\n\ndef test_zero():\n    assert add(0, 0) == 0\n\n"
                "def test_add():\n    assert add(2, 3) == 5\n"
            ),
        },
        "fix: add should add",
    )
    cfg = tmp_path / "evals.yaml"
    cfg.write_text(
        f"project:\n  repo: {repo}\n  test_paths: [tests]\nworkdir: work\n"
        "environment:\n"
        "  base_image: python:3.12-slim\n"
        "  deps_setup: pip install --no-cache-dir pytest\n"
        "  test_command: python -m pytest -q -p no:cacheprovider --junitxml=/out/junit.xml\n"
    )

    result = CliRunner().invoke(app, ["mine", "-c", str(cfg), "--no-dry-run"])

    try:
        assert result.exit_code == 0, result.output
        assert "Fail-to-pass tasks ready for review: 1" in result.output
    finally:
        images = subprocess.run(
            [
                "docker",
                "images",
                "-q",
                "--filter",
                "reference=hindsight-task",
                "--filter",
                "reference=hindsight-deps",
            ],
            capture_output=True,
            text=True,
        ).stdout.split()
        if images:
            subprocess.run(["docker", "image", "rm", "-f", *images], capture_output=True)


def test_orphan_containers_are_removed() -> None:
    from hindsight.mining.classify import remove_orphan_containers

    docker = FakeTestDocker({})
    docker.orphans = "abc123\ndef456\n"
    assert remove_orphan_containers(docker) == 2
    assert ["docker", "rm", "-f", "abc123", "def456"] in docker.calls


def test_no_orphans_means_no_removal() -> None:
    from hindsight.mining.classify import remove_orphan_containers

    docker = FakeTestDocker({})
    assert remove_orphan_containers(docker) == 0
    assert docker.removed() == 0
