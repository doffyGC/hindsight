import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from hindsight.mining.environment import (
    BuildError,
    CommandResult,
    DockerBuilder,
    DockerUnavailable,
    EnvironmentSpec,
    image_tag,
    render_dockerfile,
    render_setup_script,
)

SPEC = EnvironmentSpec(
    base_image="python:3.12-slim",
    setup='pip install -e ".[dev]" pytest',
    build_env={"B": "2", "A": "1"},
)


class FakeDocker:
    """Records docker commands and answers them from a small script of behaviours."""

    def __init__(self, *, has_image: bool = False, build_code: int = 0, build_out: str = ""):
        self.has_image = has_image
        self.build_code = build_code
        self.build_out = build_out
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str], timeout: float | None) -> CommandResult:
        args = list(args)
        self.calls.append(args)
        if args[:2] == ["docker", "version"]:
            return CommandResult(0, "27.0.0")
        if args[:3] == ["docker", "image", "inspect"]:
            return CommandResult(0, "sha256:abc\n") if self.has_image else CommandResult(1, "")
        if args[:2] == ["docker", "build"]:
            if self.build_code == 0:
                self.has_image = True
            return CommandResult(self.build_code, self.build_out)
        raise AssertionError(f"unexpected command {args}")

    def builds(self) -> int:
        return sum(1 for c in self.calls if c[:2] == ["docker", "build"])


def test_dockerfile_layout() -> None:
    dockerfile = render_dockerfile(SPEC)
    assert dockerfile.splitlines()[0] == "FROM python:3.12-slim"
    assert "apt-get install -y --no-install-recommends git" in dockerfile
    assert dockerfile.index('ENV A="1"') < dockerfile.index('ENV B="2"')
    assert "COPY work /work" in dockerfile
    assert dockerfile.rstrip().endswith("RUN bash /tmp/hindsight-setup.sh")
    assert "gold" not in dockerfile


def test_dockerfile_without_setup_or_packages() -> None:
    dockerfile = render_dockerfile(EnvironmentSpec(system_packages=()))
    assert "apt-get" not in dockerfile
    assert "hindsight-setup" not in dockerfile


def test_setup_script_fails_fast() -> None:
    assert render_setup_script(SPEC).startswith("#!/bin/bash\nset -euo pipefail\n")


def test_tag_is_stable_and_tracks_the_environment() -> None:
    sha = "8a524dfa1ea61f924afa0f58e0c16a1882410b71"
    assert image_tag(sha, SPEC) == image_tag(sha, SPEC)
    assert image_tag(sha, SPEC).startswith("hindsight-task:8a524dfa1ea6-")
    changed = EnvironmentSpec(base_image=SPEC.base_image, setup="pip install -e .")
    assert image_tag(sha, SPEC) != image_tag(sha, changed)


def test_build_writes_context_and_returns_image(tmp_path: Path) -> None:
    (tmp_path / "work").mkdir()
    docker = FakeDocker()
    image = DockerBuilder(runner=docker).build(tmp_path, "a" * 40, SPEC)
    assert (image.image_id, image.cached) == ("sha256:abc", False)
    assert docker.builds() == 1
    assert (tmp_path / "Dockerfile").read_text() == render_dockerfile(SPEC)
    assert "gold" in (tmp_path / ".dockerignore").read_text().split()
    assert (tmp_path / "build.log").exists()


def test_existing_image_is_reused(tmp_path: Path) -> None:
    docker = FakeDocker(has_image=True)
    image = DockerBuilder(runner=docker).build(tmp_path, "a" * 40, SPEC)
    assert image.cached
    assert docker.builds() == 0


def test_build_failure_reports_log_tail(tmp_path: Path) -> None:
    docker = FakeDocker(build_code=1, build_out="step 1\nstep 2\nERROR: No matching distribution\n")
    with pytest.raises(BuildError, match="No matching distribution"):
        DockerBuilder(runner=docker).build(tmp_path, "a" * 40, SPEC)
    assert "No matching distribution" in (tmp_path / "build.log").read_text()


def test_build_timeout(tmp_path: Path) -> None:
    docker = FakeDocker(build_code=124)
    with pytest.raises(BuildError, match="timed out after 5s"):
        DockerBuilder(runner=docker, timeout_s=5).build(tmp_path, "a" * 40, SPEC)


def test_unreachable_daemon() -> None:
    def runner(args: Sequence[str], timeout: float | None) -> CommandResult:
        return CommandResult(1, "Cannot connect to the Docker daemon")

    with pytest.raises(DockerUnavailable, match="Cannot connect"):
        DockerBuilder(runner=runner).check_available()


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "version"], capture_output=True).returncode == 0


@pytest.mark.skipif(not _docker_ready(), reason="needs a running Docker daemon")
def test_real_build_and_cache(tmp_path: Path) -> None:
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / "hello.py").write_text("print('ok')\n")
    (tmp_path / "gold").mkdir()
    (tmp_path / "gold" / "secret_test.py").write_text("# must not reach the image\n")
    spec = EnvironmentSpec(system_packages=(), setup="python hello.py")
    builder = DockerBuilder(timeout_s=600)
    sha = "f" * 40

    first = builder.build(tmp_path, sha, spec)
    second = builder.build(tmp_path, sha, spec)

    try:
        assert not first.cached and second.cached
        assert first.image_id == second.image_id
        listing = subprocess.run(
            ["docker", "run", "--rm", first.tag, "ls", "-A", "/work"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        assert "hello.py" in listing
        assert "secret_test.py" not in listing and "gold" not in listing
    finally:
        subprocess.run(["docker", "image", "rm", "-f", first.tag], capture_output=True)
