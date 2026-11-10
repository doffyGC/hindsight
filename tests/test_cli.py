from pathlib import Path

from typer.testing import CliRunner

from hindsight.cli import app

from .conftest import commit

runner = CliRunner()


def test_help_lists_all_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("mine", "review", "plan", "run", "report"):
        assert command in result.output


def test_unimplemented_commands_exit_2(tmp_path: Path) -> None:
    for command in ("plan", "run", "report"):
        assert runner.invoke(app, [command]).exit_code == 2


def test_mine_dry_run_reports_funnel(repo: Path, tmp_path: Path) -> None:
    commit(repo, {"pkg/core.py": "x = 2\n", "tests/test_core.py": "def t(): 2\n"}, "fix")
    commit(repo, {"pkg/core.py": "x = 3\n", "tests/test_core.py": "def t(): 3\n"}, "fix [CODEX]")
    cfg = tmp_path / "evals.yaml"
    cfg.write_text(f"project:\n  repo: {repo}\n  test_paths: [tests]\n")
    result = runner.invoke(app, ["mine", "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "Candidates touching tests and source: 2" in result.output
    assert "Excluded as AI-assisted: 1" in result.output
    assert "Kept for the next stages: 1" in result.output


def test_mine_rejects_missing_config(tmp_path: Path) -> None:
    result = runner.invoke(app, ["mine", "--config", str(tmp_path / "nope.yaml")])
    assert result.exit_code == 1


def test_mine_no_dry_run_stores_tasks_and_snapshots(repo: Path, tmp_path: Path) -> None:
    commit(repo, {"pkg/core.py": "x = 2\n", "tests/test_core.py": "def t(): 2\n"}, "fix")
    commit(repo, {"pkg/core.py": "x = 3\n", "tests/test_core.py": "def t(): 3\n"}, "fix [CODEX]")
    cfg = tmp_path / "evals.yaml"
    cfg.write_text(f"project:\n  repo: {repo}\n  test_paths: [tests]\nworkdir: work\n")

    result = runner.invoke(app, ["mine", "--config", str(cfg), "--no-dry-run", "--no-build"])

    assert result.exit_code == 0, result.output
    assert "AI-assisted commit: 1" in result.output
    assert "Tasks stored: 1" in result.output
    assert (tmp_path / "work" / "hindsight.sqlite").exists()
    task_dirs = list((tmp_path / "work" / "tasks").iterdir())
    assert len(task_dirs) == 1
    assert (task_dirs[0] / "gold" / "tests" / "test_core.py").exists()


def test_mine_limit_caps_snapshots(repo: Path, tmp_path: Path) -> None:
    for i in range(3):
        commit(repo, {"pkg/core.py": f"x = {i}0\n", "tests/test_core.py": f"# {i}\n"}, f"f{i}")
    cfg = tmp_path / "evals.yaml"
    cfg.write_text(f"project:\n  repo: {repo}\nworkdir: work\n")

    result = runner.invoke(
        app, ["mine", "-c", str(cfg), "--no-dry-run", "--no-build", "--limit", "2"]
    )

    assert result.exit_code == 0, result.output
    assert "Tasks stored: 2" in result.output


def test_mine_records_build_outcomes(repo: Path, tmp_path: Path, monkeypatch) -> None:
    from hindsight import cli
    from hindsight.mining.environment import DockerBuilder

    from .test_environment import FakeDocker

    docker = FakeDocker(build_code=1, build_out="ERROR: could not install package\n")
    monkeypatch.setattr(cli, "DockerBuilder", lambda timeout_s: DockerBuilder(runner=docker))
    commit(repo, {"pkg/core.py": "x = 2\n", "tests/test_core.py": "def t(): 2\n"}, "fix")
    cfg = tmp_path / "evals.yaml"
    cfg.write_text(f"project:\n  repo: {repo}\nworkdir: work\n")

    result = runner.invoke(app, ["mine", "-c", str(cfg), "--no-dry-run", "--no-classify"])

    assert result.exit_code == 0, result.output
    assert "build failed: ERROR: could not install package: 1" in result.output


def test_mine_stores_image_id_on_success(repo: Path, tmp_path: Path, monkeypatch) -> None:
    from hindsight import cli
    from hindsight.mining.environment import DockerBuilder
    from hindsight.store import Store

    from .test_environment import FakeDocker

    monkeypatch.setattr(cli, "DockerBuilder", lambda timeout_s: DockerBuilder(runner=FakeDocker()))
    commit(repo, {"pkg/core.py": "x = 2\n", "tests/test_core.py": "def t(): 2\n"}, "fix")
    cfg = tmp_path / "evals.yaml"
    cfg.write_text(f"project:\n  repo: {repo}\nworkdir: work\n")

    args = ["mine", "-c", str(cfg), "--no-dry-run", "--no-classify"]
    assert runner.invoke(app, args).exit_code == 0
    with Store.open(tmp_path / "work" / "hindsight.sqlite") as store:
        sha = next(iter((tmp_path / "work" / "tasks").iterdir())).name
        assert store.get_task(sha).image_digest == "sha256:abc"


def test_mine_without_docker_fails_clearly(repo: Path, tmp_path: Path, monkeypatch) -> None:
    from hindsight import cli
    from hindsight.mining.environment import CommandResult, DockerBuilder

    def no_daemon(args, timeout):
        return CommandResult(1, "Cannot connect to the Docker daemon")

    monkeypatch.setattr(cli, "DockerBuilder", lambda timeout_s: DockerBuilder(runner=no_daemon))
    cfg = tmp_path / "evals.yaml"
    cfg.write_text(f"project:\n  repo: {repo}\nworkdir: work\n")

    result = runner.invoke(app, ["mine", "-c", str(cfg), "--no-dry-run", "--no-classify"])

    assert result.exit_code == 1
    assert "--no-build" in result.output


def test_mine_builds_shared_deps_image_once(repo: Path, tmp_path: Path, monkeypatch) -> None:
    from hindsight import cli
    from hindsight.mining.environment import DockerBuilder

    from .test_environment import FakeDocker

    docker = FakeDocker()
    monkeypatch.setattr(cli, "DockerBuilder", lambda timeout_s: DockerBuilder(runner=docker))
    for i in range(2):
        commit(repo, {"pkg/core.py": f"x = {i}1\n", "tests/test_core.py": f"# {i}\n"}, f"f{i}")
    cfg = tmp_path / "evals.yaml"
    cfg.write_text(
        f"project:\n  repo: {repo}\nworkdir: work\n"
        "environment:\n  deps_setup: pip install .\n  setup: pip install -e .\n"
    )

    result = runner.invoke(app, ["mine", "-c", str(cfg), "--no-dry-run", "--no-classify"])

    assert result.exit_code == 0, result.output
    assert "Shared deps image built: hindsight-deps:" in result.output
    builds = [c for c in docker.calls if c[:2] == ["docker", "build"]]
    assert builds[0][3].startswith("hindsight-deps:")
    assert all(b[3].startswith("hindsight-task:") for b in builds[1:])
    task_dockerfile = next((tmp_path / "work" / "tasks").iterdir()) / "Dockerfile"
    assert task_dockerfile.read_text().startswith("FROM hindsight-deps:")


def _junit(*cases: tuple[str, str]) -> str:
    body = ""
    for name, status in cases:
        inner = {"passed": "", "failed": "<failure message='x'/>", "skipped": "<skipped/>"}[status]
        body += f"<testcase classname='tests.test_core' name='{name}'>{inner}</testcase>"
    return f"<testsuites><testsuite>{body}</testsuite></testsuites>"


def test_mine_classifies_fail_to_pass(repo: Path, tmp_path: Path, monkeypatch) -> None:
    from hindsight import cli
    from hindsight.mining.environment import DockerBuilder
    from hindsight.store import Store

    from .test_classify import FakeTestDocker

    docker = FakeTestDocker(
        {
            "before": (0, _junit(("test_old", "passed"), ("test_new", "failed"))),
            "after": (0, _junit(("test_old", "passed"), ("test_new", "passed"))),
        }
    )
    monkeypatch.setattr(cli, "DockerBuilder", lambda timeout_s: DockerBuilder(runner=docker))
    commit(repo, {"pkg/core.py": "x = 2\n", "tests/test_core.py": "def t(): 2\n"}, "fix")
    cfg = tmp_path / "evals.yaml"
    cfg.write_text(
        f"project:\n  repo: {repo}\nworkdir: work\n"
        "environment:\n  test_command: pytest --junitxml=/out/junit.xml\n"
    )

    result = runner.invoke(app, ["mine", "-c", str(cfg), "--no-dry-run"])

    assert result.exit_code == 0, result.output
    assert "Fail-to-pass tasks ready for review: 1" in result.output
    with Store.open(tmp_path / "work" / "hindsight.sqlite") as store:
        sha = next(iter((tmp_path / "work" / "tasks").iterdir())).name
        task = store.get_task(sha)
    assert task.f2p_tests == ["tests.test_core::test_new"]
    assert task.p2p_tests == ["tests.test_core::test_old"]


def test_mine_requires_test_command_to_classify(repo: Path, tmp_path: Path) -> None:
    cfg = tmp_path / "evals.yaml"
    cfg.write_text(f"project:\n  repo: {repo}\nworkdir: work\n")
    result = runner.invoke(app, ["mine", "-c", str(cfg), "--no-dry-run"])
    assert result.exit_code == 1
    assert "test_command" in result.output


def test_test_command_must_write_junit(repo: Path, tmp_path: Path) -> None:
    cfg = tmp_path / "evals.yaml"
    cfg.write_text(f"project:\n  repo: {repo}\nenvironment:\n  test_command: pytest\n")
    result = runner.invoke(app, ["mine", "-c", str(cfg)])
    assert result.exit_code == 1
    assert "/out/junit.xml" in result.output


def _f2p_docker():
    from .test_classify import FakeTestDocker

    return FakeTestDocker(
        {
            "before": (0, _junit(("test_old", "passed"), ("test_new", "failed"))),
            "after": (0, _junit(("test_old", "passed"), ("test_new", "passed"))),
        }
    )


def _f2p_config(repo: Path, tmp_path: Path) -> Path:
    cfg = tmp_path / "evals.yaml"
    cfg.write_text(
        f"project:\n  repo: {repo}\nworkdir: work\n"
        "environment:\n  test_command: pytest --junitxml=/out/junit.xml\n"
        "tasks:\n  issue_provider: none\n"
    )
    return cfg


def test_mine_resumes_without_redoing_classified_tasks(
    repo: Path, tmp_path: Path, monkeypatch
) -> None:
    from hindsight import cli
    from hindsight.mining.environment import DockerBuilder

    docker = _f2p_docker()
    monkeypatch.setattr(cli, "DockerBuilder", lambda timeout_s: DockerBuilder(runner=docker))
    commit(repo, {"pkg/core.py": "x = 2\n", "tests/test_core.py": "def t(): 2\n"}, "fix")
    cfg = _f2p_config(repo, tmp_path)

    assert runner.invoke(app, ["mine", "-c", str(cfg), "--no-dry-run"]).exit_code == 0
    runs_after_first = sum(1 for c in docker.calls if c[:2] == ["docker", "run"])
    second = runner.invoke(app, ["mine", "-c", str(cfg), "--no-dry-run"])

    assert second.exit_code == 0, second.output
    assert "Skipping 1 candidates already processed." in second.output
    assert sum(1 for c in docker.calls if c[:2] == ["docker", "run"]) == runs_after_first
    forced = runner.invoke(app, ["mine", "-c", str(cfg), "--no-dry-run", "--force"])
    assert sum(1 for c in docker.calls if c[:2] == ["docker", "run"]) > runs_after_first
    assert forced.exit_code == 0


def test_mine_writes_statement_from_commit_message(repo: Path, tmp_path: Path, monkeypatch) -> None:
    from hindsight import cli
    from hindsight.mining.environment import DockerBuilder
    from hindsight.store import Store

    monkeypatch.setattr(cli, "DockerBuilder", lambda timeout_s: DockerBuilder(runner=_f2p_docker()))
    message = "fix: x must be 2\n\nCallers expect x to be two, but it is one on startup."
    commit(repo, {"pkg/core.py": "x = 2\n", "tests/test_core.py": "def t(): 2\n"}, message)
    cfg = _f2p_config(repo, tmp_path)

    result = runner.invoke(app, ["mine", "-c", str(cfg), "--no-dry-run"])

    assert result.exit_code == 0, result.output
    assert "Statements written: 1" in result.output
    with Store.open(tmp_path / "work" / "hindsight.sqlite") as store:
        task = store.accepted_tasks()[0]
    assert task.statement_source == "commit"
    assert "Callers expect x to be two" in task.statement


def test_review_command_approves(repo: Path, tmp_path: Path, monkeypatch) -> None:
    from hindsight import cli
    from hindsight.mining.environment import DockerBuilder
    from hindsight.store import Store

    monkeypatch.setattr(cli, "DockerBuilder", lambda timeout_s: DockerBuilder(runner=_f2p_docker()))
    message = "fix: x must be 2\n\nCallers expect x to be two, but it is one on startup."
    commit(repo, {"pkg/core.py": "x = 2\n", "tests/test_core.py": "def t(): 2\n"}, message)
    cfg = _f2p_config(repo, tmp_path)
    assert runner.invoke(app, ["mine", "-c", str(cfg), "--no-dry-run"]).exit_code == 0

    result = runner.invoke(app, ["review", "-c", str(cfg)], input="a\n")

    assert result.exit_code == 0, result.output
    assert "Approved 1" in result.output and "Still pending: 0" in result.output
    with Store.open(tmp_path / "work" / "hindsight.sqlite") as store:
        assert store.accepted_tasks()[0].curation_status == "approved"


def test_review_without_mining_fails(tmp_path: Path, repo: Path) -> None:
    cfg = tmp_path / "evals.yaml"
    cfg.write_text(f"project:\n  repo: {repo}\nworkdir: work\n")
    assert runner.invoke(app, ["review", "-c", str(cfg)]).exit_code == 1
