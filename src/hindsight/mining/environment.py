"""Per-task Docker images (RF-02): the test environment of the parent commit.

The image holds the snapshot (`work/`) with its dependencies installed, and nothing else.
Gold tests stay out of the build context. The agent layer (Claude Code) is added later by the
`run` stage on top of this image, so mining never depends on agent credentials.

Images are tagged by commit and by a hash of the rendered Dockerfile and setup script, so an
unchanged task is never rebuilt and a changed `evals.yaml` produces a new image.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

IMAGE_REPOSITORY = "hindsight-task"
_LOG_TAIL_LINES = 3


@dataclass(frozen=True)
class EnvironmentSpec:
    """The subset of `environment:` in evals.yaml that defines the image."""

    base_image: str = "python:3.12-slim"
    system_packages: tuple[str, ...] = ("git",)
    setup: str = ""
    build_env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    output: str


Runner = Callable[[Sequence[str], float | None], CommandResult]


class BuildError(RuntimeError):
    """Raised when the image cannot be built; the message becomes the discard reason."""


class DockerUnavailable(RuntimeError):
    """Raised when the docker CLI or daemon is not reachable."""


def subprocess_runner(args: Sequence[str], timeout: float | None) -> CommandResult:
    try:
        proc = subprocess.run(
            list(args), capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        return CommandResult(124, output + f"\ntimed out after {timeout:.0f}s")
    return CommandResult(proc.returncode, proc.stdout + proc.stderr)


def render_dockerfile(spec: EnvironmentSpec) -> str:
    lines = [f"FROM {spec.base_image}"]
    if spec.system_packages:
        packages = " ".join(spec.system_packages)
        lines.append(
            "RUN apt-get update && apt-get install -y --no-install-recommends "
            f"{packages} && rm -rf /var/lib/apt/lists/*"
        )
    for key, value in sorted(spec.build_env.items()):
        lines.append(f'ENV {key}="{value}"')
    lines += ["COPY work /work", "WORKDIR /work"]
    if spec.setup.strip():
        lines += [
            "COPY .hindsight-setup.sh /tmp/hindsight-setup.sh",
            "RUN bash /tmp/hindsight-setup.sh",
        ]
    return "\n".join(lines) + "\n"


def render_setup_script(spec: EnvironmentSpec) -> str:
    return "#!/bin/bash\nset -euo pipefail\n" + spec.setup.strip() + "\n"


def image_tag(commit_sha: str, spec: EnvironmentSpec) -> str:
    fingerprint = hashlib.sha256(
        (render_dockerfile(spec) + render_setup_script(spec)).encode()
    ).hexdigest()[:10]
    return f"{IMAGE_REPOSITORY}:{commit_sha[:12]}-{fingerprint}"


def _tail(text: str) -> str:
    lines = [line for line in text.strip().splitlines() if line.strip()]
    return " | ".join(lines[-_LOG_TAIL_LINES:]) or "no output"


@dataclass(frozen=True)
class BuiltImage:
    tag: str
    image_id: str
    cached: bool


class DockerBuilder:
    def __init__(self, runner: Runner = subprocess_runner, timeout_s: float = 1800) -> None:
        self._run = runner
        self._timeout = timeout_s

    def check_available(self) -> None:
        if self._run is subprocess_runner and shutil.which("docker") is None:
            raise DockerUnavailable("docker CLI not found in PATH")
        result = self._run(["docker", "version", "--format", "{{.Server.Version}}"], 30)
        if result.returncode != 0:
            raise DockerUnavailable(f"docker daemon not reachable: {_tail(result.output)}")

    def _image_id(self, tag: str) -> str | None:
        result = self._run(["docker", "image", "inspect", "--format", "{{.Id}}", tag], 30)
        return result.output.strip() if result.returncode == 0 else None

    def build(self, task_dir: Path, commit_sha: str, spec: EnvironmentSpec) -> BuiltImage:
        """Build (or reuse) the image for one task. `task_dir` must contain `work/`."""
        tag = image_tag(commit_sha, spec)
        existing = self._image_id(tag)
        if existing:
            return BuiltImage(tag, existing, cached=True)

        (task_dir / "Dockerfile").write_text(render_dockerfile(spec))
        (task_dir / ".hindsight-setup.sh").write_text(render_setup_script(spec))
        (task_dir / ".dockerignore").write_text("gold\nbuild.log\n")

        result = self._run(["docker", "build", "-t", tag, str(task_dir)], self._timeout)
        (task_dir / "build.log").write_text(result.output)
        if result.returncode == 124:
            raise BuildError(f"build timed out after {self._timeout:.0f}s")
        if result.returncode != 0:
            raise BuildError(f"build failed: {_tail(result.output)}")

        image_id = self._image_id(tag)
        if not image_id:
            raise BuildError("build reported success but the image was not found")
        return BuiltImage(tag, image_id, cached=False)
