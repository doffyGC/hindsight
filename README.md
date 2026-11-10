# Hindsight

> Does your Claude Code configuration actually help, or is the difference just noise?

Hindsight mines a repository's git history into **verifiable eval tasks** and compares
Claude Code configurations (model, CLAUDE.md, skills, MCP servers) with **statistical rigor**.

- **Tasks with an objective answer key.** Only commits whose tests fail on the parent and pass
  on the commit (fail-to-pass) become tasks. The agent never sees those tests.
- **Statistics as a feature.** Paired design, hierarchical bootstrap confidence intervals,
  power analysis before spending, and consistency (pass^k), not single-run averages.
- **Built for individuals.** Local CLI, Docker sandboxes, mandatory budget caps.

## Status

Early development. The V1.0 requirements are defined and a manual end-to-end spike on
[tobymao/sqlglot](https://github.com/tobymao/sqlglot) succeeded. Implemented so far:

| Command | What it does | Status |
| --- | --- | --- |
| `mine` | History → fail-to-pass tasks | Done: candidates, AI filter, snapshots, images, F2P/P2P |
| `review` | Human curation of task statements | Done: approve, edit, discard, leakage hints |
| `plan` | Cost estimate and minimum detectable effect | Planned |
| `run` | Run configurations in sandboxes | Planned |
| `report` | Statistical HTML report | Planned |

## Quick start

```bash
pip install -e ".[dev]"
git clone https://github.com/tobymao/sqlglot.git ../sqlglot
hindsight mine --config examples/evals.sqlglot.yaml                         # dry run: counts only
hindsight mine --config examples/evals.sqlglot.yaml --no-dry-run --limit 5  # snapshots, images, funnel
```

Snapshots and the SQLite store go to `.hindsight/` next to the config file (`workdir` option).
Each task gets `work/` (the parent commit in a fresh one-commit repository, without
instruction files such as CLAUDE.md) and `gold/` (the commit's test files, for the evaluator).
Images come in two layers: a shared `hindsight-deps` image with all dependencies, built once,
and a thin `hindsight-task` image per task (a few MB). Gold tests never enter any image.

Each task is then classified: the gold tests run on the parent commit (before) and with the
commit's changes applied (after, twice), in containers without network. Tests that fail before
and pass in every after run are **fail-to-pass**; tests that pass in both are **pass-to-pass**;
tests whose after outcome changes are flaky and ignored. Only tasks with at least one
fail-to-pass test are kept. Use `--no-build` or `--no-classify` to stop earlier.

Mining is resumable: candidates that already reached the final stage are skipped (`--force`
redoes them). Accepted tasks get a statement from the first usable source in a cascade: the
GitHub issue or PR referenced in the commit, the commit message, or a model that only sees the
tests (`tasks.llm_command`). Then a human curates every statement:

```bash
hindsight review --config examples/evals.sqlglot.yaml
```

`review` shows each task's statement, its fail-to-pass tests and **leakage hints**: names that
the fix introduces but the tests never use, or code copied from the fix. You approve, edit (in
`$EDITOR`), discard, or view the reference fix.

## Development

```bash
pytest
ruff check . && ruff format --check .
```

Commits authored or co-authored by AI tools are excluded from mining by default, so that an
agent is not evaluated on reference solutions written by an agent.

## License

MIT. Repositories mined by Hindsight keep their own licenses; exported tasks include the
attribution those licenses require.
