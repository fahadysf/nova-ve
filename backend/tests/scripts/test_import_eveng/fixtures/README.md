# EVE-NG importer test fixtures

## Synthetic-stub-only policy (CC-1 in the consensus plan)

**Every file under this directory is hand-written by a test author.** No fixture has ever been (or should ever be) copied from a real EVE-NG / UNetLab / PNETLab install — the bytes of vendor-shipped images (Cisco IOL binaries, Juniper qcow2 disks, Arista vEOS, etc.) are copyrighted IP and cannot ship in the nova-ve repo, even sanitised.

The CI suite runs against these synthetic stubs so it stays green on stock GitHub-hosted runners with no proprietary fixtures, no privileged docker, no network egress.

## Adding a new vendor fixture

1. Write a minimal `.qcow2` / `.bin` / `.img` *named* like the vendor's canonical filename — its bytes can be any deterministic short payload (e.g. `b"synthetic-stub-for-cisco-iosv-l3"`). Adapter `match()` regexes key on filename, not on bytes.
2. Write a hand-authored `.php` or `.yml` template stanza alongside it that captures only the fields the adapter under test consults.
3. **Never** `cp` from `/opt/unetlab/...` on any operator's host.

## Layout

```
fixtures/
├── required_fields/
│   ├── all_present.json        # every required field set
│   ├── required_missing.json   # at least one required field absent
│   └── optional_missing.json   # required fields present, optional absent
├── cisco/                      # populated by per-Cisco-adapter snapshot tests (#187)
├── juniper/                    # populated by per-Juniper-adapter snapshot tests (#188)
├── arista/                     # populated by per-Arista-adapter snapshot tests (#189)
├── mikrotik/
└── vyos/
```

Each per-vendor directory holds the canonical fixtures for that vendor's adapter — currently the Cisco / Juniper / Arista / Mikrotik / VyOS PRs build their fixtures inline in the test files rather than persisting them here, so several of those subdirectories may not yet exist; they are reserved for the per-adapter snapshot fixtures that #190's full e2e test seeds via `conftest.py`.

## Why JSON for `required_fields/`

The three canonical REQUIRED_FIELDS fixtures (`all_present.json`, `required_missing.json`, `optional_missing.json`) are JSON because they directly map to the *raw intermediate dict* that parsers produce; no PHP/YAML round-trip is needed. They demonstrate the presence-only contract on `VendorAdapter.validate()` (see `test_adapters_framework.py`).
