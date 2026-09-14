# Contributing

Thanks for helping improve Google Ads MCP Safe. Small, focused fixes, documentation improvements, and reproducible bug reports are welcome. Contributions are reviewed; submission does not guarantee acceptance or a response deadline.

## Before opening a report

Read the [README](README.md), [troubleshooting guide](docs/troubleshooting.md), and [tool reference](docs/tool-reference.md). Search existing issues for the same problem. Use a bug report for unexpected behavior or a feature request to describe a missing capability and its practical benefit.

Suspected security problems go through GitHub private vulnerability reporting as described in [SECURITY.md](SECURITY.md), never in a public issue, pull request or discussion.

Never upload sign-in profiles, tokens, keys, real account identifiers, customer information, advertising results, or raw provider responses. Use synthetic examples and remove private details from logs and screenshots.

## Local development

Use Python 3.12 or newer. The [README](README.md) describes the exact environments tested so far; do not assume all platforms are verified.

In the macOS Terminal app, from your copy of this project's root folder, create an isolated environment and install the development tools:

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

Installation can download dependencies. No Google credentials are needed for the offline test suite. In the same Terminal window and project folder, run:

```sh
python -m pytest -q
python -m ruff check .
```

The tests use simulated provider calls and synthetic accounts. Some tests enable write settings inside those simulations to verify safeguards; this is not permission to enable writes against a real account. Preserve the test protections that block real provider construction and network access. Do not add tests that load real credentials or contact Google by default.

## Proposing a change

Open a pull request, a proposed set of changes for review, with the problem, resulting behavior, and verification results. Keep changes focused and explain any effect on account permissions, write safeguards, credential handling, or audit records. Update the relevant documentation when behavior changes. Include a regression test for a bug fix when it can meaningfully demonstrate the failure and correction.

Report the commands you ran and their actual results. Separate offline tests from any separately authorized live checks; simulations do not prove that Google accepted a request. A code contribution does not authorize live account changes, credential changes, or publication.

Only submit material you have the right to contribute. Contributions are provided under this project's [MIT license](LICENSE).

## Working together

Be respectful, critique the work rather than the person, and keep discussions relevant. Harassment, threats, discriminatory abuse, and sharing private information are not acceptable. Maintainers may remove disruptive content or restrict participation.
