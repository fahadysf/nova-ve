# Ubuntu 26.04 LTS ISO Provenance

Current baseline media for the first nova-ve deployment lane:

- Filename: `ubuntu-26.04-live-server-amd64.iso`
- Official release page: `https://releases.ubuntu.com/26.04/`
- Official checksum file: `https://releases.ubuntu.com/26.04/SHA256SUMS`
- Expected SHA256: `dec49008a71f6098d0bcfc822021f4d042d5f2db279e4d75bdd981304f1ca5d9`

## Acquisition

1. Download the ISO from the official Ubuntu release page.
2. Download the matching checksum file from the same official location.

## Checksum Verification

Example:

```bash
cd ~/Downloads
sha256sum ubuntu-26.04-live-server-amd64.iso
grep 'ubuntu-26.04-live-server-amd64.iso' SHA256SUMS
```

The two SHA256 values must match exactly before the image is used for validation.

## Base OS Rule

Use the Ubuntu 26.04 LTS GA AMD64 server ISO as the base OS for the supported deployment lane. Do not use pre-release media for new installs.

## Refresh Rule

When an official `26.04.x` point-release AMD64 server ISO becomes available from Ubuntu's official release channels:

1. update this document
2. rerun the validation lane using the newer official image

Do not switch to daily or unofficial media for the main validation lane.
