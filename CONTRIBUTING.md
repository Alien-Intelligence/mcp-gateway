# Contributing

This GitHub repository is a read-only mirror of the canonical repository, which is hosted on a private GitLab instance. Pull requests opened here are not merged directly.

## Issues

Issues are welcome on GitHub: bug reports, questions about configuration, documentation gaps, feature requests. For a bug, include the gateway version, the resolver type, the relevant configuration (with secrets removed) and the log lines or the response that show the problem. Report security problems as described in [SECURITY.md](SECURITY.md), never in a public issue.

## Code contributions

1. Open an issue first and describe the change, so we can agree on the approach before you write code.
2. Open a pull request against `main` once the issue is agreed. A maintainer reviews it here, imports it into the canonical repository with your authorship kept, and the change comes back to this mirror with the next release. The pull request is then closed with a link to the release.
3. One logical change per pull request, with its tests.

### Development

```bash
uv sync --extra dev
uv run ruff check src tests scripts dev
uv run ruff format --check src tests scripts dev
uv run pyright && uv run pyright scripts tests/e2e
uv run pytest tests/unit -q
```

All of these must pass. Do not silence a linter or type checker with `# noqa` or `# type: ignore`: fix the cause.

### Conventions

- Conventional Commits: `feat:`, `fix:`, `docs:`, `test:`, `ci:`, `build:`, `chore:`. Only `feat` produces a minor release; everything else is a patch.
- Do not change version numbers: releases are cut by the maintainers.
- No real keys, tokens or customer data in code, tests, fixtures or documentation. Use obviously fake values such as `fake-alien-key-alice` and `example.com`.
- Configuration is validated strictly: a missing or invalid required value is an error, not a silent default.
