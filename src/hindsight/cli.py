"""Command-line interface. Each command mirrors one stage of the pipeline (spec, section 4)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import typer

from hindsight import __version__
from hindsight.config import Config, ConfigError, load_config
from hindsight.mining.candidates import Candidate, list_candidates
from hindsight.mining.classify import GoldTestError, GoldTestRunner, remove_orphan_containers
from hindsight.mining.environment import BuildError, DockerBuilder, DockerUnavailable
from hindsight.mining.snapshot import SnapshotError, build_snapshot, export_tree
from hindsight.mining.statements import StatementWriter, github_repo_from_remote
from hindsight.review import open_in_editor, reviewer_name, run_review
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


def _github_repo(cfg: Config) -> str | None:
    if cfg.tasks.issue_provider != "github":
        return None
    return cfg.tasks.github_repo or github_repo_from_remote(cfg.project.repo)


def _test_diff(cfg: Config, candidate: Candidate) -> str:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(cfg.project.repo),
            "diff",
            candidate.parent,
            candidate.sha,
            "--",
            *candidate.test_files,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout


def _write_statements(
    cfg: Config, store: Store, candidates: list[Candidate], refresh: bool = False
) -> tuple[int, int]:
    """Give accepted tasks a statement from the cascade (RF-07).

    By default only tasks without one; with `refresh`, every task still pending review whose
    statement was not written by a human. Returns (written, discarded as AI-assisted).
    """
    by_sha = {c.sha: c for c in candidates}
    pending = [
        t
        for t in store.accepted_tasks(curation_status="pending")
        if t.commit_sha in by_sha
        and (not t.statement or (refresh and t.statement_source != "human"))
    ]
    if not pending:
        return 0, 0
    writer = StatementWriter(
        sources=cfg.tasks.statement_sources,
        ai_patterns=cfg.tasks.ai_patterns,
        github_repo=_github_repo(cfg),
        min_commit_chars=cfg.tasks.min_commit_message_chars,
        llm_command=cfg.tasks.llm_command,
    )
    written = ai_discarded = 0
    for task in pending:
        candidate = by_sha[task.commit_sha]
        statement = writer.write(
            candidate.subject, candidate.message, task.f2p_tests, _test_diff(cfg, candidate)
        )
        if statement is None:
            continue
        if statement.ai_disclosed and not cfg.tasks.include_ai_assisted:
            reason = "AI-assisted commit (disclosed in the issue or PR)"
            store.log(task.commit_sha, Stage.AI_FILTER, Outcome.DISCARDED, reason)
            ai_discarded += 1
            continue
        store.set_statement(task.commit_sha, statement.text, statement.source)
        written += 1
    return written, ai_discarded


def _final_stage(build: bool, classify: bool) -> Stage:
    if classify:
        return Stage.FAIL_TO_PASS
    return Stage.BUILD if build else Stage.SNAPSHOT


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
    force: bool = typer.Option(
        False, help="Reprocess candidates that already reached the final stage."
    ),
    refresh_statements: bool = typer.Option(
        False, help="Rewrite statements of tasks pending review (human edits are kept)."
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

    if builder is not None:
        removed = remove_orphan_containers(builder.runner)
        if removed:
            typer.echo(f"Removed {removed} test containers left by an interrupted run.")
    deps_image = _build_deps(cfg, builder) if builder is not None else None

    with Store.open(cfg.store_path) as store:
        for candidate in candidates:
            store.log(candidate.sha, Stage.CANDIDATE, Outcome.PASSED)
            if candidate.ai_assisted and not cfg.tasks.include_ai_assisted:
                store.log(candidate.sha, Stage.AI_FILTER, Outcome.DISCARDED, "AI-assisted commit")
            elif store.outcome(candidate.sha, Stage.AI_FILTER) is None:
                # Never overwrite a discard made after reading the issue or PR text.
                store.log(candidate.sha, Stage.AI_FILTER, Outcome.PASSED)

        final = _final_stage(build, classify)
        pending = [c for c in kept if force or store.outcome(c.sha, final) is None]
        if len(pending) < len(kept):
            typer.echo(f"Skipping {len(kept) - len(pending)} candidates already processed.")
        selected = pending[:limit] if limit else pending
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

        if tests is not None:
            written, ai_discarded = _write_statements(cfg, store, candidates, refresh_statements)
            if written:
                typer.echo(f"Statements written: {written}")
            if ai_discarded:
                typer.echo(f"Discarded as AI-assisted after reading the issue/PR: {ai_discarded}")

        _print_funnel(store)
        typer.echo(f"\nTasks stored: {store.count_tasks()} (in {cfg.store_path})")
        if tests is not None:
            accepted = next(r.passed for r in store.funnel() if r.stage == Stage.FAIL_TO_PASS)
            typer.echo(f"Fail-to-pass tasks ready for review: {accepted}")


@app.command()
def review(config: Path = CONFIG_OPTION) -> None:
    """Approve, edit or discard each fail-to-pass task's statement (RF-09 to RF-11)."""
    cfg = _load(config)
    if not cfg.store_path.exists():
        typer.secho("No mining results yet; run 'hindsight mine --no-dry-run' first.", fg="red")
        raise typer.Exit(code=1)
    with Store.open(cfg.store_path) as store:
        summary = run_review(
            store,
            cfg.task_dir,
            prompt=lambda text: typer.prompt(text, default="s", show_default=False),
            echo=typer.echo,
            editor=open_in_editor,
            reviewer=reviewer_name(cfg.project.repo),
            github_repo=_github_repo(cfg),
        )
        remaining = len(store.accepted_tasks(curation_status="pending"))
        approved = len(store.accepted_tasks(curation_status="approved"))
    typer.echo(
        f"\nApproved {summary.approved}, discarded {summary.discarded}, "
        f"skipped {summary.skipped}. Total approved: {approved}. Still pending: {remaining}."
    )


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
