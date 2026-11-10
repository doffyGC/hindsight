"""Command-line interface. Each command mirrors one stage of the pipeline (spec, section 4)."""

from __future__ import annotations

from pathlib import Path

import typer

from hindsight import __version__
from hindsight.config import Config, ConfigError, load_config
from hindsight.mining.candidates import Candidate, list_candidates
from hindsight.mining.snapshot import SnapshotError, build_snapshot
from hindsight.store import Outcome, Stage, Store, Task

app = typer.Typer(
    help="Mine git history into verifiable eval tasks and compare Claude Code configurations.",
    no_args_is_help=True,
)

CONFIG_OPTION = typer.Option(Path("evals.yaml"), "--config", "-c", help="Path to evals.yaml.")


def _not_implemented(command: str) -> None:
    typer.secho(f"'{command}' is not implemented yet (see the V1.0 spec).", fg="yellow", err=True)
    raise typer.Exit(code=2)


def _load(config: Path) -> Config:
    try:
        return load_config(config)
    except ConfigError as exc:
        typer.secho(f"Invalid config: {exc}", fg="red", err=True)
        raise typer.Exit(code=1) from exc


def _print_funnel(store: Store) -> None:
    typer.echo("\nMining funnel")
    for row in store.funnel():
        if row.passed == 0 and row.discarded == 0:
            continue
        typer.echo(f"  {row.stage:<14} passed {row.passed:>6}   discarded {row.discarded:>6}")
        for reason, count in row.reasons.items():
            typer.echo(f"      - {reason}: {count}")


def _snapshot_one(cfg: Config, store: Store, candidate: Candidate) -> None:
    try:
        snap = build_snapshot(
            cfg.project.repo,
            candidate,
            cfg.task_dir(candidate.sha),
            strip_files=cfg.environment.strip_instruction_files,
        )
    except SnapshotError as exc:
        store.log(candidate.sha, Stage.SNAPSHOT, Outcome.DISCARDED, str(exc))
        return
    store.log(candidate.sha, Stage.SNAPSHOT, Outcome.PASSED)
    store.upsert_task(
        Task(
            commit_sha=candidate.sha,
            parent_sha=candidate.parent,
            commit_date=candidate.date,
            subject=candidate.subject,
            gold_test_files=list(snap.gold_test_files),
            snapshot_path=str(snap.workdir),
        )
    )


@app.command()
def version() -> None:
    """Print the Hindsight version."""
    typer.echo(__version__)


@app.command()
def mine(
    config: Path = CONFIG_OPTION,
    dry_run: bool = typer.Option(
        True, help="Only list candidates. Use --no-dry-run to record them and build snapshots."
    ),
    limit: int | None = typer.Option(
        None, min=1, help="Build snapshots for at most this many candidates (newest first)."
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

    if dry_run:
        typer.echo(f"Candidates touching tests and source: {len(candidates)}")
        typer.echo(f"Excluded as AI-assisted: {len(candidates) - len(kept)}")
        typer.echo(f"Kept for the next stages: {len(kept)}")
        return

    with Store.open(cfg.store_path) as store:
        for candidate in candidates:
            store.log(candidate.sha, Stage.CANDIDATE, Outcome.PASSED)
            if candidate.ai_assisted and not cfg.tasks.include_ai_assisted:
                store.log(candidate.sha, Stage.AI_FILTER, Outcome.DISCARDED, "AI-assisted commit")
            else:
                store.log(candidate.sha, Stage.AI_FILTER, Outcome.PASSED)

        selected = kept[:limit] if limit else kept
        with typer.progressbar(selected, label="Building snapshots") as progress:
            for candidate in progress:
                _snapshot_one(cfg, store, candidate)

        _print_funnel(store)
        typer.echo(f"\nTasks stored: {store.count_tasks()} (in {cfg.store_path})")
    typer.secho("Next stages (Docker build and fail-to-pass) are not implemented yet.", fg="yellow")


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
