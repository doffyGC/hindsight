"""Command-line interface. Each command mirrors one stage of the pipeline (spec, section 4)."""

from __future__ import annotations

from pathlib import Path

import typer

from hindsight import __version__
from hindsight.config import Config, ConfigError, load_config
from hindsight.mining.candidates import Candidate, list_candidates
from hindsight.mining.classify import GoldTestError, GoldTestRunner
from hindsight.mining.environment import BuildError, DockerBuilder, DockerUnavailable
from hindsight.mining.snapshot import SnapshotError, build_snapshot, export_tree
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


def _snapshot_one(cfg: Config, store: Store, candidate: Candidate) -> bool:
    try:
        snap = build_snapshot(
            cfg.project.repo,
            candidate,
            cfg.task_dir(candidate.sha),
            strip_files=cfg.environment.strip_instruction_files,
        )
    except SnapshotError as exc:
        store.log(candidate.sha, Stage.SNAPSHOT, Outcome.DISCARDED, str(exc))
        return False
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
    return True


def _build_deps(cfg: Config, builder: DockerBuilder) -> str | None:
    """Build the shared deps image once; return its tag (None when not configured)."""
    spec = cfg.environment.spec
    if not spec.deps_setup.strip():
        return None
    context = cfg.workdir / "deps"
    try:
        tree = export_tree(cfg.project.repo, spec.deps_ref, context / "src")
        image = builder.build_deps(context, spec, tree)
    except (SnapshotError, BuildError) as exc:
        typer.secho(f"Shared deps image failed: {exc}", fg="red", err=True)
        typer.echo(f"Build log: {context / 'build.log'}", err=True)
        raise typer.Exit(code=1) from exc
    state = "reused" if image.cached else "built"
    typer.echo(f"Shared deps image {state}: {image.tag} (from {spec.deps_ref})")
    return image.tag


def _build_one(
    cfg: Config,
    store: Store,
    builder: DockerBuilder,
    candidate: Candidate,
    deps_image: str | None,
) -> str | None:
    """Build the task image; return its tag, or None when the task was discarded."""
    try:
        image = builder.build(
            cfg.task_dir(candidate.sha), candidate.sha, cfg.environment.spec, deps_image
        )
    except BuildError as exc:
        store.log(candidate.sha, Stage.BUILD, Outcome.DISCARDED, _reason(str(exc)))
        return None
    store.log(candidate.sha, Stage.BUILD, Outcome.PASSED, "cached" if image.cached else None)
    task = store.get_task(candidate.sha)
    if task is not None:
        task.image_digest = image.image_id
        store.upsert_task(task)
    return image.tag


def _classify_one(
    cfg: Config, store: Store, tests: GoldTestRunner, candidate: Candidate, image: str
) -> None:
    try:
        result = tests.classify_task(
            image, cfg.task_dir(candidate.sha), stability_runs=cfg.tasks.stability_runs
        )
    except GoldTestError as exc:
        store.log(candidate.sha, Stage.FAIL_TO_PASS, Outcome.DISCARDED, _reason(str(exc)))
        return
    task = store.get_task(candidate.sha)
    if task is not None:
        task.f2p_tests, task.p2p_tests = list(result.f2p), list(result.p2p)
        store.upsert_task(task)
    if result.accepted:
        store.log(candidate.sha, Stage.FAIL_TO_PASS, Outcome.PASSED)
    else:
        store.log(candidate.sha, Stage.FAIL_TO_PASS, Outcome.DISCARDED, result.discard_reason())


def _reason(message: str, limit: int = 160) -> str:
    """Keep discard reasons short so the funnel groups similar failures."""
    return message if len(message) <= limit else message[: limit - 1] + "…"


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
        None, min=1, help="Process at most this many candidates (newest first)."
    ),
    build: bool = typer.Option(
        True, help="Build a Docker image per task. Use --no-build to stop after snapshots."
    ),
    classify: bool = typer.Option(
        True, help="Classify fail-to-pass tests. Use --no-classify to stop after the images."
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

    classify = classify and build
    if classify and not cfg.environment.test_command:
        typer.secho(
            "environment.test_command is required to classify tasks (or pass --no-classify).",
            fg="red",
            err=True,
        )
        raise typer.Exit(code=1)
    builder = DockerBuilder(timeout_s=cfg.environment.build_timeout_s) if build else None
    if builder is not None:
        try:
            builder.check_available()
        except DockerUnavailable as exc:
            typer.secho(f"{exc}. Start Docker or pass --no-build.", fg="red", err=True)
            raise typer.Exit(code=1) from exc

    deps_image = _build_deps(cfg, builder) if builder is not None else None

    with Store.open(cfg.store_path) as store:
        for candidate in candidates:
            store.log(candidate.sha, Stage.CANDIDATE, Outcome.PASSED)
            if candidate.ai_assisted and not cfg.tasks.include_ai_assisted:
                store.log(candidate.sha, Stage.AI_FILTER, Outcome.DISCARDED, "AI-assisted commit")
            else:
                store.log(candidate.sha, Stage.AI_FILTER, Outcome.PASSED)

        selected = kept[:limit] if limit else kept
        tests = (
            GoldTestRunner(
                builder.runner, cfg.environment.test_command, cfg.environment.test_timeout_s
            )
            if classify and builder is not None
            else None
        )
        label = "Mining tasks" if tests else ("Snapshots and images" if builder else "Snapshots")
        with typer.progressbar(selected, label=label) as progress:
            for candidate in progress:
                if not _snapshot_one(cfg, store, candidate) or builder is None:
                    continue
                image = _build_one(cfg, store, builder, candidate, deps_image)
                if image is not None and tests is not None:
                    _classify_one(cfg, store, tests, candidate, image)

        _print_funnel(store)
        typer.echo(f"\nTasks stored: {store.count_tasks()} (in {cfg.store_path})")
        if tests is not None:
            accepted = next(r.passed for r in store.funnel() if r.stage == Stage.FAIL_TO_PASS)
            typer.echo(f"Fail-to-pass tasks ready for review: {accepted}")


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
