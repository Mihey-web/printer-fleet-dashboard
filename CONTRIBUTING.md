# Contributing

Thanks for your interest in improving this project! Contributions of all kinds
are welcome — bug reports, fixes, features, docs, and tests.

## Getting started

1. Fork the repository and create a feature branch.
2. Set up a local environment:
   ```bash
   python -m venv .venv
   . .venv/bin/activate        # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   pip install pytest
   ```
3. Make your change with a clear, focused commit history.
4. Run the test suite and keep it green:
   ```bash
   python -m pytest -q
   ```
5. Open a pull request describing what changed and why.

## Guidelines

- Keep changes surgical and well-scoped; one concern per PR.
- Add or update tests for behavior changes.
- Never commit secrets, real IP addresses, access codes, serials, or credentials.
  The fleet, Telegram token, and proxies live in the database, not in the repo.
- Match the existing code style (standard library + FastAPI conventions).

## Contributor License Agreement (CLA)

To keep the project's dual-licensing model workable (AGPL-3.0 for the community
plus an optional commercial license — see [`COMMERCIAL-LICENSE.md`](./COMMERCIAL-LICENSE.md)),
contributors must agree to the following when submitting a contribution:

> By submitting a contribution (a pull request, patch, or any other work) to this
> project, you certify that you are the author of the contribution (or are
> authorized to submit it) and you grant the project's copyright holder a
> perpetual, worldwide, non-exclusive, royalty-free, irrevocable license to use,
> reproduce, modify, sublicense, and distribute your contribution, **including
> the right to license it under both the AGPL-3.0 and separate commercial
> license terms**. You retain copyright to your contribution and may use it
> elsewhere freely.

This lets the copyright holder relicense the combined work commercially while
your contribution remains available to everyone under the AGPL-3.0. If you cannot
agree to these terms, please open an issue to discuss before contributing.

Opening a pull request is taken as acceptance of the CLA above.
