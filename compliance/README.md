# Compliance — component inventories

Third-party component inventories for AiNxt Enterprise, generated from the
**actual installed dependency trees** rather than from the manifests, so they
reflect what really ships.

| File | Contents |
|---|---|
| [`python-components.tsv`](python-components.tsv) | 208 Python distributions installed in the gateway image |
| [`node-components.tsv`](node-components.tsv) | 683 npm packages in the `ai-ui` dependency tree |
| [`generate-sbom.sh`](generate-sbom.sh) | Regenerates both from a running stack |

Regenerate after any dependency change:

```bash
./compliance/generate-sbom.sh
```

Project licensing itself is in [`../LICENSE`](../LICENSE) (MIT) and
[`../NOTICE`](../NOTICE).

---

### 1. Copyleft check

No GPL, AGPL, SSPL or BUSL components were found in either tree. The Python
tree does carry weak-copyleft LGPL components (`psycopg2-binary`, `psycopg`,
`psycopg-binary`, `psycopg-pool`, and the optional `ldap3`) — all used
unmodified via pip, which does not trigger LGPL's copyleft obligations. See
[`../THIRD-PARTY-NOTICES.md`](../THIRD-PARTY-NOTICES.md) §1.5 for the full
legal basis.

Node licences are overwhelmingly permissive:

```
  MIT                      576
  ISC                      50
  Apache-2.0               18
  MPL-2.0                  12
  BSD-3-Clause             11
  BSD-2-Clause             8
  0BSD                     2
  MIT-0                    1
```

MPL-2.0 (12 packages) is file-level copyleft and generally fine for
redistribution without relicensing, but worth confirming with your own counsel.

### 3. "UNKNOWN" entries in the Python inventory — RESOLVED (2026-09-05)

All 74 previously-`UNKNOWN` rows have been manually resolved via PyPI's
`license_expression` metadata field (cross-checked against the upstream
GitHub repo's license for the handful of packages PyPI itself had no license
metadata for: `fsspec`, `google-crc32c`, `mypy_extensions`, `pgvector`,
`setuptools`). All resolved entries are permissive (MIT/BSD/Apache-2.0/PSF-2.0
family). `psycopg`, `psycopg-binary`, and `psycopg-pool` were also caught by
the same metadata gap and are now correctly labeled `LGPL-3.0-only` in the TSV,
matching what §1.5 already documented separately. If you need a fully
independent, tool-generated SBOM in addition to this manual resolution,
PEP 639-aware tooling is still an option:

```bash
pip install cyclonedx-bom && cyclonedx-py environment -o compliance/python-sbom.json
```

---

## Scope

These inventories cover the Platform (this repository) only. `ainxt-cli` and
`ainxt-os` ship their own third-party inventories
(`THIRD-PARTY-NOTICES` and `THIRD_PARTY_INVENTORY.yaml` respectively).
