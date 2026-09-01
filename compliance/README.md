# Compliance — component inventories

Third-party component inventories for AiNxt Enterprise, generated from the
**actual installed dependency trees** rather than from the manifests, so they
reflect what really ships.

| File | Contents |
|---|---|
| [`python-components.tsv`](python-components.tsv) | 224 Python distributions installed in the gateway image |
| [`node-components.tsv`](node-components.tsv) | 683 npm packages in the `ai-ui` dependency tree |
| [`generate-sbom.sh`](generate-sbom.sh) | Regenerates both from a running stack |

Regenerate after any dependency change:

```bash
./compliance/generate-sbom.sh
```

Project licensing itself is in [`../LICENSE`](../LICENSE) (Apache-2.0) and
[`../NOTICE`](../NOTICE).

---

## Items that need a licensing decision before public release

### 1. NVIDIA proprietary components (7 packages)

`requirements.txt` pulls the full CUDA stack as a **hard dependency**, not an
optional extra. 7 of those distributions carry
`LicenseRef-NVIDIA-Proprietary`:

```
  nvidia-cuda-cupti            13.0.85
  nvidia-cuda-nvrtc            13.0.88
  nvidia-cufft                 12.0.0.61
  nvidia-cufile                1.15.1.6
  nvidia-curand                10.4.0.35
  nvidia-cusolver              12.0.4.66
  nvidia-cusparse              12.6.3.3
```

They are installed on **every** deployment, including CPU-only and arm64 hosts
where they can never be used — the audit measured the resulting gateway image at
**10.7 GB**. Two things follow:

- **Licensing.** Shipping an Apache-2.0 project whose default install pulls
  proprietary NVIDIA libraries needs an explicit decision and, if kept, a note
  in `NOTICE`. It is not an Apache-2.0-compatible licence.
- **Practicality.** A CPU-only extra (or moving `torch` and the CUDA stack behind
  a `[gpu]` extra) would cut install size and time substantially and remove the
  question entirely for most users.

### 2. Copyleft check

No GPL, AGPL, SSPL or BUSL components were found in either tree. Node licences
are overwhelmingly permissive:

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

### 3. "UNKNOWN" entries in the Python inventory

Roughly a third of Python rows read `UNKNOWN`. This is a **metadata-extraction
limitation, not a red flag**: those packages declare licences via PEP 639
`License-Expression` or a `licenses/` directory rather than the legacy
`License:` field or a trove classifier. Spot-checked examples — `fastapi` (MIT),
`anyio` (MIT), `cryptography` (Apache-2.0 OR BSD-3-Clause), `attrs` (MIT) — are
all permissive. If you need a legally reviewable SBOM, generate one with a tool
that resolves PEP 639, e.g.:

```bash
pip install cyclonedx-bom && cyclonedx-py environment -o compliance/python-sbom.json
```

---

## Scope

These inventories cover the Platform (this repository) only. `ainxt-cli` and
`ainxt-os` ship their own third-party inventories
(`THIRD-PARTY-NOTICES` and `THIRD_PARTY_INVENTORY.yaml` respectively).
