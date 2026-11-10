"""Loading and validating evals.yaml (spec, section 8). Only the fields used so far are typed."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from hindsight.mining.environment import EnvironmentSpec

DEFAULT_AI_PATTERNS = [
    r"\[(CLAUDE|CODEX|GEMINI|DEEPSEEK|COPILOT|CURSOR)\]",
    r"(?im)^co-authored-by:.*(claude|anthropic|codex|openai|copilot|gemini|cursor)",
]


class ConfigError(ValueError):
    """Raised when evals.yaml is missing or invalid."""


@dataclass(frozen=True)
class ProjectConfig:
    repo: Path
    since: str | None = None
    test_paths: list[str] = field(default_factory=lambda: ["tests"])


@dataclass(frozen=True)
class TasksConfig:
    include_ai_assisted: bool = False
    ai_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_AI_PATTERNS))


@dataclass(frozen=True)
class EnvironmentConfig:
    strip_instruction_files: list[str] = field(default_factory=lambda: ["CLAUDE.md", "AGENTS.md"])
    spec: EnvironmentSpec = field(default_factory=EnvironmentSpec)
    build_timeout_s: float = 1800


@dataclass(frozen=True)
class Config:
    project: ProjectConfig
    tasks: TasksConfig
    environment: EnvironmentConfig
    workdir: Path
    raw: dict[str, Any]

    @property
    def store_path(self) -> Path:
        return self.workdir / "hindsight.sqlite"

    def task_dir(self, commit_sha: str) -> Path:
        return self.workdir / "tasks" / commit_sha


def load_config(path: Path) -> Config:
    if not path.exists():
        raise ConfigError(f"{path} not found")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("top level must be a mapping")

    project_raw = raw.get("project") or {}
    if "repo" not in project_raw:
        raise ConfigError("project.repo is required")
    repo = (path.parent / str(project_raw["repo"])).resolve()
    since = project_raw.get("since")
    if since == "auto":
        # TODO: derive from the training cutoff of the configured models (RF-01).
        raise ConfigError("project.since: 'auto' is not implemented yet; use an ISO date")

    tasks_raw = raw.get("tasks") or {}
    env_raw = raw.get("environment") or {}
    strip = env_raw.get("strip_instruction_files")
    return Config(
        project=ProjectConfig(
            repo=repo,
            since=str(since) if since else None,
            test_paths=list(project_raw.get("test_paths") or ["tests"]),
        ),
        tasks=TasksConfig(
            include_ai_assisted=bool(tasks_raw.get("include_ai_assisted", False)),
            ai_patterns=list(tasks_raw.get("ai_patterns") or DEFAULT_AI_PATTERNS),
        ),
        environment=EnvironmentConfig(
            strip_instruction_files=(
                list(strip) if strip is not None else ["CLAUDE.md", "AGENTS.md"]
            ),
            spec=_environment_spec(env_raw),
            build_timeout_s=float(env_raw.get("build_timeout_s", 1800)),
        ),
        workdir=(path.parent / str(raw.get("workdir", ".hindsight"))).resolve(),
        raw=raw,
    )


def _environment_spec(env_raw: dict[str, Any]) -> EnvironmentSpec:
    defaults = EnvironmentSpec()
    build_env = env_raw.get("build_env") or {}
    if not isinstance(build_env, dict):
        raise ConfigError("environment.build_env must be a mapping")
    packages = env_raw.get("system_packages")
    return EnvironmentSpec(
        base_image=str(env_raw.get("base_image", defaults.base_image)),
        system_packages=(
            tuple(str(p) for p in packages) if packages is not None else defaults.system_packages
        ),
        setup=str(env_raw.get("setup", "")),
        build_env={str(k): str(v) for k, v in build_env.items()},
    )
