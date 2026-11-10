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
| `mine` | History → fail-to-pass tasks | Candidate listing and AI-assisted commit detection |
| `review` | Human curation of task statements | Planned |
| `plan` | Cost estimate and minimum detectable effect | Planned |
| `run` | Run configurations in sandboxes | Planned |
| `report` | Statistical HTML report | Planned |

## Quick start

```bash
pip install -e ".[dev]"
git clone https://github.com/tobymao/sqlglot.git ../sqlglot
hindsight mine --config examples/evals.sqlglot.yaml
```

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
