---
title: nova-ve
hide:
  - navigation
---

# nova-ve

> A clean-room network virtualization environment built with modern tooling.

!!! warning "Pre-Alpha Development Version"
    This project is not fit for actual lab use yet. Expect incomplete features, broken workflows, and data/runtime instability.

## Quick install

On a fresh Ubuntu 26.04 LTS x86_64 host with sudo access:

```bash
curl -fsSL https://raw.githubusercontent.com/fahadysf/nova-ve/main/install.sh | sudo bash
```

The script provisions Docker, PostgreSQL, Caddy, the Python backend, the SvelteKit frontend, Guacamole for HTML5 consoles, `dnsmasq`/`nftables`, and the privileged network helper — then drops systemd units and starts everything. It is idempotent: re-running fast-forwards the repo, reinstalls the helper, reconciles existing NAT-Cloud bridge state, and restarts services without rotating existing secrets.

After install, open `http://<host>/` and log in with the admin credentials printed at the end of the run (also written to `nova-ve-install-summary.md` in the directory you launched the installer from).

Docs are published at `docs.nova-ve.com` and mirrored to GitHub Pages.

[Full quick-start walk-through →](getting-started/quick-start.md){ .md-button .md-button--primary }
[Migrating from EVE-NG? →](importing-eveng/index.md){ .md-button }

## What is nova-ve?

nova-ve emulates a network of virtual devices (routers, switches, firewalls, hosts) on a single Linux host. Devices run as QEMU/KVM VMs or Docker containers. Networks between devices are Linux bridges or OVS bridges. The web UI gives you a topology editor that wires devices together, plus HTML5 consoles for each running device.

| Layer | Technology |
|-------|-----------|
| Frontend | Svelte 5 + SvelteKit + Svelte Flow + Tailwind CSS |
| Backend | FastAPI + Pydantic + SQLAlchemy (async) |
| Database | PostgreSQL (app data) + JSON files (lab topology) |
| Virtualization | QEMU/KVM + Docker |
| Console | Apache Guacamole (HTML5) |
| Reverse proxy | Caddy |
| Base OS | Ubuntu 26.04 LTS |

## Where next?

<div class="grid cards" markdown>

- :material-rocket-launch:{ .lg .middle } **[Getting started](getting-started/index.md)**

    ---

    Install nova-ve, log in, and orient yourself.

- :material-network:{ .lg .middle } **[User guides](user-guides/index.md)**

    ---

    Build your first lab, add nodes, wire them together, use NAT-Cloud, and open consoles.

- :material-import:{ .lg .middle } **[Importing from EVE-NG](importing-eveng/index.md)**

    ---

    Migrate images and templates from an existing EVE-NG / PNETLab host.

- :material-tools:{ .lg .middle } **[Operations](operations/index.md)**

    ---

    Back up labs, upgrade in place, and troubleshoot a stuck install.

- :material-book-open-variant:{ .lg .middle } **[Reference](reference/index.md)**

    ---

    CLI flags, filesystem layout, glossary.

- :material-code-braces:{ .lg .middle } **[Development](development/index.md)**

    ---

    Architecture, codebase tour, contributor flow.

</div>
