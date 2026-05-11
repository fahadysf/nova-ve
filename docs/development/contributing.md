# Contributing

The canonical contributor guide lives at [`CONTRIBUTING.md`](https://github.com/fahadysf/nova-ve/blob/main/CONTRIBUTING.md) in the repo root so GitHub's "Open a PR" flow surfaces it. The summary below mirrors the highlights for site visitors.

## Contribution model

Fork → branch → push → PR against `main`. Direct pushes to `main` are reserved for maintainers.

## Before you start

- Skim the project [README](https://github.com/fahadysf/nova-ve/blob/main/README.md) and the [Architecture overview](architecture.md).
- Open an issue to discuss large changes before implementing.
- Preserve the clean-room approach: no copying from upstream proprietary platforms.

## Local development

=== "Backend"

    ```bash
    cd backend
    python -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt
    PYTHONPATH=. .venv/bin/pytest
    ```

=== "Frontend"

    ```bash
    cd frontend
    npm install
    npm run check
    npm run build
    ```

## Pull request expectations

- Explain *what* and *why*; reference the related issue.
- Mention design tradeoffs.
- Include verification evidence: backend test output, `npm run check`, `npm run build`, and manual test notes for un-automated behavior.

## Commit style

Small, coherent commits with descriptive messages. Structured commit trailers (decision records) are preferred — maintainers may squash on merge.

## Licensing

Contributions are accepted under [Apache License 2.0](https://github.com/fahadysf/nova-ve/blob/main/LICENSE). By submitting a PR you represent that the contribution is your original work and does not include proprietary code from incompatible sources.
