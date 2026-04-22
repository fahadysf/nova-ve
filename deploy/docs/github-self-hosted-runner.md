# GitHub Self-Hosted Runner

This repository now expects the CI workflow to run on a labeled self-hosted runner instead of GitHub-hosted `ubuntu-latest`.

The workflow is intentionally limited to `push` on `main` plus `workflow_dispatch`. It does **not** run `pull_request` on the privileged self-hosted runner by default.

Required labels:

- `self-hosted`
- `linux`
- `x64`
- `docker`
- `nova-ve-ci`

## Recommended Runner VM

- OS: `Ubuntu 26.04 x86_64`
- CPU: `4 vCPU` minimum
- RAM: `8 GB` minimum
- Disk: `60 GB` minimum
- Network: outbound internet access for GitHub, npm, PyPI, and container image pulls

Use a dedicated runner VM rather than the eventual deployment target when possible.

## Bootstrap

On the runner VM:

```bash
cd /path/to/nova-ve
sudo bash deploy/scripts/install-github-runner-prereqs.sh
```

That installs:

- Docker Engine + Compose plugin
- Python `3.12` + `venv`
- Node.js + npm
- git, curl, jq, and build prerequisites

## Register The Runner

1. Open the GitHub repository settings:
   - `Settings -> Actions -> Runners -> New self-hosted runner`
2. Choose:
   - `Linux`
   - `x64`
3. Log into the VM as `github-runner`.
4. Download the runner package GitHub provides for that page.
5. Configure it with the required labels:

```bash
./config.sh \
  --url https://github.com/fahadysf/nova-ve \
  --token <runner-registration-token> \
  --labels self-hosted,linux,x64,docker,nova-ve-ci \
  --name nova-ve-ci-01
```

6. Install and start the runner service:

```bash
sudo ./svc.sh install github-runner
sudo ./svc.sh start
```

## Validation

Confirm the runner is online in GitHub, then trigger the workflow manually with `workflow_dispatch`.

Expected capabilities:

- `docker compose config -q`
- `docker compose up -d db`
- backend `pytest`, import, and `compileall`
- frontend `npm ci`, `npm run check`, and `npm run build`

## Operational Notes

- Do not route untrusted `pull_request` jobs onto this runner while it has Docker-capable host access.
- The workflow now includes a `docker compose down -v || true` cleanup step before the compose smoke test to reduce stale self-hosted-runner state.
- The runner user must remain in the `docker` group.
- Keep the runner patched, and avoid using it as a shared general-purpose VM.
