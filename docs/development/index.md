# Development

Contributor and architecture content. If you are an operator running labs, the rest of this site is what you want — this section assumes you are reading or modifying the codebase.

## Start here

- [Architecture overview](architecture.md) — the big picture: services, data flow, where state lives.
- [Backend](backend.md) — FastAPI app structure, services, the EVE-NG importer module.
- [Frontend](frontend.md) — SvelteKit app structure, the canvas, design system.

## Specific subsystems

- [Node add/edit workflow](node-add-edit-workflow.md) — modal + backend contract for creating and editing nodes.
- [NAT-Cloud network](nat-cloud-network.md) — routed lab network design with DHCP and outbound NAT.
- [Design system adoption](design-system.md) — how the bundled design tokens propagated through the app.
- [Deployment contract](deployment-contract.md) — what every host-side install must satisfy, what may not change without a contract bump.

## CI

- [Self-hosted runner](ci/self-hosted-runner.md) — how the GitHub Actions self-hosted runner is set up.
- [Ubuntu 26.04 beta ISO](ci/ubuntu-2604-iso.md) — base image notes for the CI runner.
- [fy-lab validation](ci/fy-lab-validation.md) — the in-house validation lab.

## Contributing

See [`CONTRIBUTING.md`](https://github.com/fahadysf/nova-ve/blob/main/CONTRIBUTING.md) in the repo root for the canonical contributor guide (it lives at the root so GitHub's "Open a PR" flow surfaces it). [Local mirror →](contributing.md).
