"""Docker images for tasks (RF-02).

Two layers keep disk usage low:

- a shared **deps image**, built once from a recent tree of the repository, with the system
  packages, build environment and `deps_setup` (e.g. installing all dependencies);
- a thin **task image** per task, `FROM` the deps image, that copies the snapshot (`work/`) and
  runs `setup` (e.g. `pip install -e .`, which only fetches what changed).

Without `deps_setup`, the task image starts from the base image and does everything itself.
Gold tests and the reference patch never enter any image. The agent layer (Claude Code) is
added later by the `run` stage, so mining never depends on agent credentials.

Tags are content hashes, so unchanged images are reused and a changed `evals.yaml` rebuilds.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

TASK_REPOSITORY = "hindsight-task"
DEPS_REPOSITORY = "hindsight-deps"
_LOG_TAIL_LINES = 3


@dataclass(frozen=True)
class EnvironmentSpec:
    """The subset of `environment:` in evals.yaml that defines the images."""

    base_image: str = "python:3.12-slim"
    system_packages: tuple[str, ...] = ("git",)
    build_env: dict[str, str] = field(default_factory=dict)
    deps_setup: str = ""  # shared layer; empty disables it
    deps_ref: str = "HEAD"  # tree used to build the shared layer
    setup: str = ""  # per task


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    output: str


Runner = Callable[[Sequence[str], float | None], CommandResult]


class BuildError(RuntimeError):
    """Raised when an image cannot be built; the message becomes the discard reason."""


class DockerUnavailable(RuntimeError):
    """Raised when the docker CLI or daemon is not reachable."""


def subprocess_runner(args: Sequence[str], timeout: float | None) -> CommandResult:
    try:
        proc = subprocess.run(
            list(args), capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout if isinstance(exc.stdout, str) else ""
        return CommandResult(124, output + f"\ntimed out after {timeout:.0f}s")
    return CommandResult(proc.returncode, proc.stdout + proc.stderr)


def _script(commands: str) -> str:
    return "#!/bin/bash\nset -euo pipefail\n" + commands.strip() + "\n"


def _system_layers(spec: EnvironmentSpec) -> list[str]:
    lines = [f"FROM {spec.base_image}"]
    if spec.system_packages:
        lines.append(
            "RUN apt-get update && apt-get install -y --no-install-recommends "
            f"{' '.join(spec.system_packages)} && rm -rf /var/lib/apt/lists/*"
        )
    lines += [f'ENV {key}="{value}"' for key, value in sorted(spec.build_env.items())]
    return lines


def render_deps_dockerfile(spec: EnvironmentSpec) -> str:
    lines = _system_layers(spec) + [
        "COPY src /deps-src",
        "COPY .hindsight-setup.sh /tmp/hindsight-setup.sh",
        "RUN cd /deps-src && bash /tmp/hindsight-setup.sh",
    ]
    return "\n".join(lines) + "\n"


def render_task_dockerfile(spec: EnvironmentSpec, deps_image: str | None) -> str:
    lines = [f"FROM {deps_image}"] if deps_image else _system_layers(spec)
    lines += ["COPY work /work", "WORKDIR /work"]
    if spec.setup.strip():
        lines += [
            "COPY .hindsight-setup.sh /tmp/hindsight-setup.sh",
            "RUN bash /tmp/hindsight-setup.sh",
        ]
    return "\n".join(lines) + "\n"


def _fingerprint(*parts: str) -> str:
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()[:10]


def deps_tag(spec: EnvironmentSpec, tree_id: str) -> str:
    return (
        f"{DEPS_REPOSITORY}:"
        f"{_fingerprint(render_deps_dockerfile(spec), _script(spec.deps_setup), tree_id)}"
    )


def task_tag(commit_sha: str, spec: EnvironmentSpec, deps_image: str | None) -> str:
    dockerfile = render_task_dockerfile(spec, deps_image)
    return f"{TASK_REPOSITORY}:{commit_sha[:12]}-{_fingerprint(dockerfile, _script(spec.setup))}"


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

    @property
    def runner(self) -> Runner:
        return self._run

    def check_available(self) -> None:
        if self._run is subprocess_runner and shutil.which("docker") is None:
            raise DockerUnavailable("docker CLI not found in PATH")
        result = self._run(["docker", "version", "--format", "{{.Server.Version}}"], 30)
        if result.returncode != 0:
            raise DockerUnavailable(f"docker daemon not reachable: {_tail(result.output)}")

    def _image_id(self, tag: str) -> str | None:
        result = self._run(["docker", "image", "inspect", "--format", "{{.Id}}", tag], 30)
        return result.output.strip() if result.returncode == 0 else None

    def _build(self, context: Path, tag: str, dockerfile: str, setup: str) -> BuiltImage:
        existing = self._image_id(tag)
        if existing:
            return BuiltImage(tag, existing, cached=True)
        (context / "Dockerfile").write_text(dockerfile)
        (context / ".hindsight-setup.sh").write_text(_script(setup))
        (context / ".dockerignore").write_text("gold\nreference\nbuild.log\n")
        result = self._run(["docker", "build", "-t", tag, str(context)], self._timeout)
        (context / "build.log").write_text(result.output)
        if result.returncode == 124:
            raise BuildError(f"build timed out after {self._timeout:.0f}s")
        if result.returncode != 0:
            raise BuildError(f"build failed: {_tail(result.output)}")
        image_id = self._image_id(tag)
        if not image_id:
            raise BuildError("build reported success but the image was not found")
        return BuiltImage(tag, image_id, cached=False)

    def build_deps(self, context: Path, spec: EnvironmentSpec, tree_id: str) -> BuiltImage:
        """Build (or reuse) the shared deps image. `context` must contain `src/`."""
        return self._build(
            context, deps_tag(spec, tree_id), render_deps_dockerfile(spec), spec.deps_setup
        )

    def build(
        self,
        task_dir: Path,
        commit_sha: str,
        spec: EnvironmentSpec,
        deps_image: str | None = None,
    ) -> BuiltImage:
        """Build (or reuse) the image for one task. `task_dir` must contain `work/`."""
        return self._build(
            task_dir,
            task_tag(commit_sha, spec, deps_image),
            render_task_dockerfile(spec, deps_image),
            spec.setup,
        )
