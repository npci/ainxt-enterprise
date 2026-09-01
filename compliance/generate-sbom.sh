#!/usr/bin/env bash
# Regenerate the component inventories in this directory.
#
# Reads the ACTUAL installed dependency trees rather than the manifests, so the
# output reflects what ships. Requires the stack to be built:
#   ./install.sh   (or: docker compose up -d --build)
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> Python components (from the gateway image)"
cat > /tmp/_sbom.py <<'PY'
import importlib.metadata as md
rows=set()
for d in md.distributions():
    m=d.metadata
    name=m.get("Name") or "?"
    lic=(m.get("License") or "").strip()
    lic=lic.splitlines()[0][:60] if lic else ""
    if not lic or lic.lower() in ("unknown","none"):
        cl=[c for c in (m.get_all("Classifier") or []) if c.startswith("License ::")]
        lic=cl[0].split("::")[-1].strip()[:60] if cl else "UNKNOWN"
    rows.add((name, d.version or "?", lic))
for n,v,l in sorted(rows, key=lambda r: r[0].lower()):
    print(f"{n}\t{v}\t{l}")
PY
docker cp /tmp/_sbom.py ainxt-gateway:/tmp/_sbom.py
{ printf 'name\tversion\tlicense\n'; docker exec ainxt-gateway python /tmp/_sbom.py; } \
  > compliance/python-components.tsv
echo "    $(($(wc -l < compliance/python-components.tsv) - 1)) components"

echo "==> Node components (from the ai-ui dependency tree)"
docker run --rm -v "$PWD/ai-ui:/src:ro" node:20-alpine sh -c '
  cd /tmp && cp /src/package.json . && npm install --package-lock-only --no-audit --no-fund >/dev/null 2>&1
  node -e "
    const l=require(\"/tmp/package-lock.json\"); const out=[];
    for (const [k,v] of Object.entries(l.packages||{})) {
      if (!k.startsWith(\"node_modules/\")) continue;
      out.push([k.replace(/^node_modules\//,\"\"), v.version||\"?\", v.license||\"UNKNOWN\"].join(\"\t\"));
    }
    console.log([...new Set(out)].sort().join(\"\n\"));
  "' > /tmp/_node.tsv
{ printf 'name\tversion\tlicense\n'; cat /tmp/_node.tsv; } > compliance/node-components.tsv
echo "    $(($(wc -l < compliance/node-components.tsv) - 1)) components"

echo "==> Done. Review compliance/README.md for the flagged items."
