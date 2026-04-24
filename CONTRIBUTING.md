# Contributing to nova-ve

Thanks for your interest in contributing.

`nova-ve` is currently a pre-alpha clean-room reimplementation effort. Please
expect rough edges, incomplete features, and active architecture changes.

## Contribution Model

This project uses the standard open source workflow:

1. Fork the repository
2. Create a branch in your fork
3. Make your changes
4. Push your branch to your fork
5. Open a pull request against `main`

Direct pushes to the upstream repository are reserved for project maintainers.

## Before You Start

- Read [README.md](README.md)
- Read [README.md / CONTRIBUTING.md](README.md / CONTRIBUTING.md) for project context and conventions
- Review the relevant documents in `research/` before changing API behavior
- Prefer discussing large changes in an issue before starting implementation

## Ground Rules

- Keep diffs focused and reviewable
- Do not mix unrelated changes in one pull request
- Do not add new dependencies without strong justification
- Preserve the clean-room approach
  - do not copy code from legacy platform or other proprietary sources
  - work from observed behavior, public docs, and original implementation only
- Never commit secrets, credentials, or private keys

## Development Workflow

### Backend

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Run backend checks:

```bash
cd backend
PYTHONPATH=. .venv/bin/pytest
```

### Frontend

```bash
cd frontend
npm install
npm run check
npm run build
```

### Database / Local Services

For the broader local environment and deployment tooling, use the scripts under
`deploy/scripts/`.

## Pull Request Expectations

A good PR should:

- explain what changed and why
- reference the related issue when applicable
- mention any design tradeoffs
- include verification evidence

Please include, at minimum:

- backend test results if backend code changed
- frontend `npm run check` results if frontend code changed
- frontend `npm run build` results if frontend code changed
- any manual test notes for behavior that is not yet automated

## Commit Guidance

Commits should be small, coherent, and easy to review.

This repository uses structured commit messages with decision-record trailers.
If you are contributing through a PR, clean, descriptive commits are preferred
even if maintainers later squash or rewrite history.

## Licensing

By submitting a contribution, you agree that your contribution may be included
in this project under the Apache License 2.0.

You also represent that:

- you have the right to submit the code
- the contribution is your original work, or you otherwise have the legal right
  to contribute it
- the contribution does not knowingly include proprietary code copied from
  incompatible sources

## Areas Where Help Is Useful

- API compatibility with legacy platform behavior
- frontend lab UX and topology editing
- Docker/QEMU runtime reliability
- Guacamole integration and console UX
- deployment hardening for Ubuntu hosts
- tests and regression coverage

## Questions

If you are unsure whether a change is in scope, open an issue first and outline:

- the problem
- the proposed approach
- any compatibility impact
- how you plan to test it
