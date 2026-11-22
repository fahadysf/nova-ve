# GitHub Self-Hosted Runner

The CI workflow (`.github/workflows/ci.yml`) currently runs on GitHub-hosted `ubuntu-latest` runners and triggers on `push` to `main` and `pull_request` targeting `main`. This page documents how to register a **self-hosted runner** and switch the workflow to it (change `runs-on: ubuntu-latest` to the label set below) if you want CI on your own hardware.

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
- Host-native Ubuntu `26.04` `python3` + `venv`
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

Expected capabilities (what the current workflow needs):

- backend `pytest`, import, and `compileall` (no live database — the suite runs without one)
- frontend `npm install`, `npm run check`, `npm test`, and `npm run build`

## Operational Notes

- Do not route untrusted `pull_request` jobs onto this runner while it has Docker-capable host access.
- The runner user must remain in the `docker` group.
- Keep the runner patched, and avoid using it as a shared general-purpose VM.
