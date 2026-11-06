# Contributing to SOFIA

Follow the Windows setup in [README.md](README.md), install the `dev` extra,
and run the pytest suite before opening a pull request.

- Keep changes focused on one issue and link it in the pull request.
- Use clear names and type hints where they clarify an interface. Prefer the
  standard library and explain any new dependencies.
- Add deterministic tests for behavior changes. Keep the test suite independent
  of external services and credentials.
- Keep credentials, local environment files, and generated files out of Git.

## Package boundaries

The CLI lives in `src/sofia/cli.py`. The following packages reserve boundaries
for future work.

| Package | Intended responsibility |
| --- | --- |
| `core` | Assistant orchestration |
| `providers` | AI service integrations |
| `tools` | Desktop automation and developer tools |
| `security` | Authorization and safety checks |
| `storage` | Persistence and memory |
| `ui` | Desktop user interface |
| `voice` | Speech input and output |
