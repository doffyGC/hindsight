"""Command-line interface. Each command mirrors one stage of the pipeline (spec, section 4)."""

from __future__ import annotations

from pathlib import Path

import typer

from hindsight import __version__
from hindsight.config import ConfigError, load_config
from hindsight.mining.candidates import list_candidates

app = typer.Typer(
    help="Mine git history into verifiable eval tasks and compare Claude Code configurations.",
    no_args_is_help=True,
)

CONFIG_OPTION = typer.Option(Path("evals.yaml"), "--config", "-c", help="Path to evals.yaml.")


def _not_implemented(command: str) -> None:
    typer.secho(f"'{command}' is not implemented yet (see the V1.0 spec).", fg="yellow", err=True)
    raise typer.Exit(code=2)


def _load(config: Path):
    try:
        return load_config(config)
    except ConfigError as exc:
        typer.secho(f"Invalid config: {exc}", fg="red", err=True)
        raise typer.Exit(code=1) from exc


@app.command()
def version() -> None:
    """Print the Hindsight version."""
    typer.echo(__version__)


@app.command()
def mine(
    config: Path = CONFIG_OPTION,
    dry_run: bool = typer.Option(
        True, help="Only list candidate commits (snapshot, build and F2P are not implemented yet)."
    ),
) -> None:
    """Mine the git history into fail-to-pass tasks (RF-01 to RF-08, RF-24 to RF-27)."""
    cfg = _load(config)
    candidates = list_candidates(
        repo=cfg.project.repo,
        since=cfg.project.since,
        test_paths=cfg.project.test_paths,
        ai_patterns=cfg.tasks.ai_patterns,
    )
    kept = [c for c in candidates if not c.ai_assisted or cfg.tasks.include_ai_assisted]
    excluded_ai = len(candidates) - len(kept)
    typer.echo(f"Candidates touching tests and source: {len(candidates)}")
    typer.echo(f"Excluded as AI-assisted: {excluded_ai}")
    typer.echo(f"Kept for the next stages: {len(kept)}")
    if not dry_run:
        _not_implemented("mine (snapshot/build/F2P)")


@app.command()
def review(config: Path = CONFIG_OPTION) -> None:
    """Curate task statements interactively (RF-09 to RF-11)."""
    _not_implemented("review")


@app.command()
def plan(config: Path = CONFIG_OPTION) -> None:
    """Estimate cost and minimum detectable effect before spending (RF-12 to RF-14, RF-28)."""
    _not_implemented("plan")


@app.command()
def run(config: Path = CONFIG_OPTION) -> None:
    """Run every configuration on every task k times (RF-15 to RF-19, RF-29, RF-30)."""
    _not_implemented("run")


@app.command()
def report(config: Path = CONFIG_OPTION) -> None:
    """Build the statistical HTML report from the SQLite store (RF-20 to RF-23)."""
    _not_implemented("report")


if __name__ == "__main__":
    app()
