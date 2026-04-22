# Ubuntu 26.04 Beta ISO Provenance

Current baseline media for the first nova-ve deployment lane:

- Filename: `ubuntu-26.04-beta-live-server-amd64.iso`
- Official release page: `https://releases.ubuntu.com/26.04/`
- Official beta index: `https://cdimage.ubuntu.com/releases/26.04/beta/`

## Acquisition

1. Download the ISO from the official Ubuntu release page or beta image index.
2. Download the matching checksum file from the same official location.

## Checksum Verification

Example:

```bash
cd ~/Downloads
sha256sum ubuntu-26.04-beta-live-server-amd64.iso
grep 'ubuntu-26.04-beta-live-server-amd64.iso' SHA256SUMS
```

The two SHA256 values must match exactly before the image is used for FY Lab validation.

## Refresh Rule

When an official `26.04` RC or GA AMD64 server ISO becomes available from Ubuntu's official release channels:

1. update this document
2. update the FY Lab runbook
3. rerun Pass A using the newer official image

Do not switch to daily or unofficial media for the main validation lane.
