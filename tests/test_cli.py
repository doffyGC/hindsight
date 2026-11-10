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
    for command in ("review", "plan", "run", "report"):
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

    result = runner.invoke(app, ["mine", "--config", str(cfg), "--no-dry-run"])

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

    result = runner.invoke(app, ["mine", "--config", str(cfg), "--no-dry-run", "--limit", "2"])

    assert result.exit_code == 0, result.output
    assert "Tasks stored: 2" in result.output
