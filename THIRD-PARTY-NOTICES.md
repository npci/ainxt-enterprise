# Third-Party Notices — AiNxt Platform

**Project:** AiNxt Platform  
**Copyright:** Copyright 2026 National Payments Corporation of India  
**Project License:** MIT  
**Generated:** 2026-09-02 (Section 1.7 added 2026-09-04)  
**Scope:** All Python runtime dependencies (`requirements.txt`, `requirements-ocr.txt`) and
Node.js dependencies (`ai-ui/`, `desktop/`) shipped with or installed by this project, plus
the CodeWiki build stage's separate Python 3.12 environment (git-installed via `Dockerfile`,
outside `requirements.txt` — see Section 1.7).

> This file is a **legal artifact** maintained by hand and reviewed at each release.
> It supplements `NOTICE` and does not replace it. Full text of the non-permissive
> licenses referenced below: LGPL-3.0 at https://www.gnu.org/licenses/lgpl-3.0.txt,
> MPL-2.0 at https://www.mozilla.org/en-US/MPL/2.0/.

---

## Section 1 — Python Runtime Dependencies

Source: `requirements.txt` + installed dependency tree (see `compliance/python-components.tsv`).

### 1.1 MIT Licensed

| Dependency | Version | License | Source / Repository |
|---|---|---|---|
| aiohttp-retry | 2.9.1 | MIT | https://github.com/inyutin/aiohttp_retry |
| anthropic | 0.100.0 | MIT | https://github.com/anthropic-ai/anthropic-sdk-python |
| APScheduler | 3.11.3 | MIT | https://github.com/agronholm/apscheduler |
| beautifulsoup4 | 4.14.3 | MIT | https://www.crummy.com/software/BeautifulSoup/ |
| camelot-py | 2.0.0 | MIT | https://github.com/camelot-dev/camelot |
| charset-normalizer | 3.5.1 | MIT | https://github.com/Ousret/charset_normalizer |
| click | 8.5.0 | MIT (BSD-style) | https://github.com/pallets/click |
| croniter | 6.2.2 | MIT | https://github.com/kiorky/croniter |
| Deprecated | 1.3.1 | MIT | https://github.com/tantale/deprecated |
| docstring_parser | 0.18.0 | MIT | https://github.com/rr-/docstring_parser |
| et_xmlfile | 2.0.0 | MIT | https://foss.heptapod.net/openpyxl/et_xmlfile |
| h11 | 0.16.0 | MIT | https://github.com/python-hyper/h11 |
| Jinja2 | 3.1.6 | BSD-3-Clause | https://github.com/pallets/jinja |
| kafka-python | 2.3.2 | Apache-2.0 | https://github.com/dpkp/kafka-python |
| limits | 5.8.0 | MIT | https://github.com/alisaifee/limits |
| lxml | 6.1.0 | BSD-3-Clause | https://github.com/lxml/lxml |
| mammoth | 1.11.0 | BSD-2-Clause | https://github.com/mwilliamson/python-mammoth |
| Markdown | 3.10.2 | BSD-style | https://github.com/Python-Markdown/markdown |
| markdown-it-py | 4.2.0 | MIT | https://github.com/executablebooks/markdown-it-py |
| markdownify | 1.2.3 | MIT | https://github.com/matthewwithanm/python-markdownify |
| markitdown | 0.1.7 | MIT | https://github.com/microsoft/markitdown |
| mcp | 2.1.1 | MIT | https://github.com/modelcontextprotocol/python-sdk |
| mcp-types | 2.1.1 | MIT | https://github.com/modelcontextprotocol/python-sdk |
| mdurl | 0.1.2 | MIT | https://github.com/executablebooks/mdurl |
| minio | 7.2.20 | Apache-2.0 | https://github.com/minio/minio-py |
| networkx | 3.6.1 | BSD-3-Clause | https://github.com/networkx/networkx |
| ollama | 0.6.1 | MIT | https://github.com/ollama/ollama-python |
| onnxruntime | 1.26.0 | MIT | https://github.com/microsoft/onnxruntime |
| openpyxl | 3.1.5 | MIT | https://foss.heptapod.net/openpyxl/openpyxl |
| opt-einsum | 3.3.0 | MIT | https://github.com/dgasmith/opt_einsum |
| orjson | 3.11.9 | Apache-2.0 | https://github.com/ijl/orjson |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause | https://github.com/pypa/packaging |
| passlib | 1.7.4 | BSD-2-Clause | https://github.com/efficks/passlib |
| pdfplumber | 0.11.9 | MIT | https://github.com/jsvine/pdfplumber |
| pgvector | 0.4.2 | MIT | https://github.com/pgvector/pgvector-python |
| pluggy | 1.6.0 | MIT | https://github.com/pytest-dev/pluggy |
| prometheus_client | 0.25.0 | Apache-2.0 | https://github.com/prometheus/client_python |
| pyclipper | 1.4.0 | MIT | https://github.com/fonttools/pyclipper |
| pydantic-settings | 2.14.2 | MIT | https://github.com/pydantic/pydantic-settings |
| PyJWT | 2.13.0 | MIT | https://github.com/jpadilla/pyjwt |
| python-docx | 1.2.0 | MIT | https://github.com/python-openxml/python-docx |
| python-dotenv | 1.2.2 | BSD-3-Clause | https://github.com/theskumar/python-dotenv |
| python-pptx | 1.0.2 | MIT | https://github.com/scanny/python-pptx |
| PyYAML | 6.0.3 | MIT | https://github.com/yaml/pyyaml |
| redis | 7.1.1 | MIT | https://github.com/redis/redis-py |
| reportlab | 4.5.1 | BSD-style | https://www.reportlab.com/opensource/ |
| rich | 15.0.0 | MIT | https://github.com/Textualize/rich |
| rq | 2.7.0 | BSD-2-Clause | https://github.com/rq/rq |
| six | 1.17.0 | MIT | https://github.com/benjaminp/six |
| slowapi | 0.1.9 | MIT | https://github.com/laurentS/slowapi |
| sniffio | 1.3.1 | MIT OR Apache-2.0 | https://github.com/python-trio/sniffio |
| soupsieve | 2.9.2 | MIT | https://github.com/facelessuser/soupsieve |
| SQLAlchemy | 2.0.52 | MIT | https://github.com/sqlalchemy/sqlalchemy |
| striprtf | 0.0.26 | BSD-3-Clause | https://github.com/joshy/striprtf |
| structlog | 25.5.0 | Apache-2.0 | https://github.com/hynek/structlog |
| tabulate | 0.10.0 | MIT | https://github.com/astanin/python-tabulate |
| tenacity | 9.1.4 | Apache-2.0 | https://github.com/jd/tenacity |
| tiktoken | 0.12.0 | MIT | https://github.com/openai/tiktoken |
| tqdm | 4.70.0 | MPL-2.0 AND MIT | https://github.com/tqdm/tqdm |
| tree-sitter | 0.25.2 | MIT | https://github.com/tree-sitter/py-tree-sitter |
| triton | 3.7.1 | MIT | https://github.com/openai/triton |
| uvloop | 0.22.1 | MIT | https://github.com/MagicStack/uvloop |
| watchfiles | 1.2.0 | MIT | https://github.com/samuelcolvin/watchfiles |
| wheel | 0.45.1 | MIT | https://github.com/pypa/wheel |
| xlrd | 2.0.2 | BSD-3-Clause | https://github.com/python-excel/xlrd |
| xlsxwriter | 3.2.9 | BSD-2-Clause | https://github.com/jmcnamara/XlsxWriter |

### 1.2 Apache-2.0 Licensed

| Dependency | Version | License | Source / Repository | NOTICE Required? |
|---|---|---|---|---|
| aiohttp | 3.14.3 | Apache-2.0 AND MIT | https://github.com/aio-libs/aiohttp | Yes |
| aiosignal | 1.4.0 | Apache-2.0 | https://github.com/aio-libs/aiosignal | Yes |
| bcrypt | 4.0.1 | Apache-2.0 | https://github.com/pyca/bcrypt | Yes |
| distro | 1.9.0 | Apache-2.0 | https://github.com/python-distro/distro | Yes |
| docker | 7.1.0 | Apache-2.0 | https://github.com/docker/docker-py | Yes |
| fire | 0.7.1 | Apache-2.0 | https://github.com/google/python-fire | Yes |
| flatbuffers | 25.12.19 | Apache-2.0 | https://github.com/google/flatbuffers | Yes |
| frozenlist | 1.8.0 | Apache-2.0 | https://github.com/aio-libs/frozenlist | Yes |
| google-api-core | 2.34.0 | Apache-2.0 | https://github.com/googleapis/python-api-core | Yes |
| google-auth | 2.57.0 | Apache-2.0 | https://github.com/googleapis/google-auth-library-python | Yes |
| google-cloud-bigquery | 3.44.0 | Apache-2.0 | https://github.com/googleapis/python-bigquery | Yes |
| google-cloud-core | 2.7.0 | Apache-2.0 | https://github.com/googleapis/python-cloud-core | Yes |
| google-genai | 1.75.0 | Apache-2.0 | https://github.com/googleapis/python-genai | Yes |
| google-resumable-media | 2.10.2 | Apache-2.0 | https://github.com/googleapis/google-resumable-media-python | Yes |
| googleapis-common-protos | 1.75.2 | Apache-2.0 | https://github.com/googleapis/proto-plus-python | Yes |
| hf-xet | 1.6.0 | Apache-2.0 | https://github.com/huggingface/xet-core | Yes |
| huggingface_hub | 1.29.0 | Apache-2.0 | https://github.com/huggingface/huggingface_hub | Yes |
| magika | 0.6.3 | Apache-2.0 | https://github.com/google/magika | Yes |
| multidict | 6.7.1 | Apache-2.0 | https://github.com/aio-libs/multidict | Yes |
| openai | 2.36.0 | Apache-2.0 | https://github.com/openai/openai-python | Yes |
| opencv-contrib-python | 4.11.0.86 | Apache-2.0 | https://github.com/opencv/opencv-python | Yes |
| opencv-python | 4.11.0.86 | Apache-2.0 | https://github.com/opencv/opencv-python | Yes |
| opencv-python-headless | 4.11.0.86 | Apache-2.0 | https://github.com/opencv/opencv-python | Yes |
| paddleocr | 2.10.0 | Apache-2.0 | https://github.com/PaddlePaddle/PaddleOCR | Yes (optional) |
| paddlepaddle | 3.2.2 | Apache-2.0 | https://github.com/PaddlePaddle/Paddle | Yes (optional) |
| proto-plus | 1.28.4 | Apache-2.0 | https://github.com/googleapis/proto-plus-python | Yes |
| propcache | 0.5.2 | Apache-2.0 | https://github.com/aio-libs/propcache | Yes |
| python-multipart | 0.0.31 | Apache-2.0 | https://github.com/Kludex/python-multipart | Yes |
| rapidocr-onnxruntime | 1.4.4 | Apache-2.0 | https://github.com/RapidAI/RapidOCR | Yes |
| requests | 2.33.1 | Apache-2.0 | https://github.com/psf/requests | Yes |
| safetensors | 0.8.0 | Apache-2.0 | https://github.com/huggingface/safetensors | Yes |
| sentence-transformers | 6.0.0 | Apache-2.0 | https://github.com/UKPLab/sentence-transformers | Yes |
| simsimd | 6.5.16 | Apache-2.0 | https://github.com/ashvardanian/SimSIMD | Yes |
| stringzilla | 5.1.2 | Apache-2.0 | https://github.com/ashvardanian/StringZilla | Yes |
| tokenizers | 0.23.1 | Apache-2.0 | https://github.com/huggingface/tokenizers | Yes |
| transformers | 5.16.1 | Apache-2.0 | https://github.com/huggingface/transformers | Yes |
| tzdata | 2026.3 | Apache-2.0 | https://github.com/python/tzdata | Yes |
| yarl | 1.24.5 | Apache-2.0 | https://github.com/aio-libs/yarl | Yes |

### 1.3 BSD Licensed

| Dependency | Version | License | Source / Repository |
|---|---|---|---|
| contourpy | 1.3.3 | BSD-3-Clause | https://github.com/contourpy/contourpy |
| GitPython | 3.1.40+ | BSD-3-Clause | https://github.com/gitpython-developers/GitPython |
| httpx | 0.28.1 | BSD-3-Clause | https://github.com/encode/httpx |
| httpcore | 1.0.9 | BSD-3-Clause | https://github.com/encode/httpcore |
| lmdb | 2.3.0 | OLDAP-2.8 (OpenLDAP) | https://github.com/jnwatson/py-lmdb |
| matplotlib | 3.10.9 | PSF-compatible | https://github.com/matplotlib/matplotlib |
| mpmath | 1.3.0 | BSD-3-Clause | https://github.com/fredrik-johansson/mpmath |
| numpy | 1.26.4 | BSD-3-Clause | https://github.com/numpy/numpy |
| pandas | 2.3.3 | BSD-3-Clause | https://github.com/pandas-dev/pandas |
| passlib | 1.7.4 | BSD-2-Clause | https://github.com/efficks/passlib |
| pillow | 12.3.0 | HPND (MIT-style) | https://github.com/python-pillow/Pillow |
| protobuf | 7.36.0 | BSD-3-Clause | https://github.com/protocolbuffers/protobuf |
| pyasn1 | 0.6.4 | BSD-2-Clause | https://github.com/pyasn1/pyasn1 |
| pyasn1_modules | 0.4.2 | BSD-2-Clause | https://github.com/pyasn1/pyasn1-modules |
| pycryptodome | 3.23.0 | BSD-2-Clause AND Public Domain | https://github.com/Legrandin/pycryptodome |
| pydantic | 2.13.4 | MIT | https://github.com/pydantic/pydantic |
| pypdf | 6.16.1 | BSD-3-Clause | https://github.com/py-pdf/pypdf |
| pypdfium2 | 4.30.0 | Apache-2.0 OR BSD-3-Clause | https://github.com/pypdfium2-team/pypdfium2 |
| python-dateutil | 2.9.0.post0 | Apache-2.0 AND BSD-3-Clause | https://github.com/dateutil/dateutil |
| python-dotenv | 1.2.2 | BSD-3-Clause | https://github.com/theskumar/python-dotenv |
| rsa | — | Apache-2.0 | https://github.com/sybrenstuvel/python-rsa |
| scipy | 1.17.1 | BSD-3-Clause | https://github.com/scipy/scipy |
| scikit-image | 0.26.0 | BSD-3-Clause | https://github.com/scikit-image/scikit-image |
| scikit-learn | 1.8.0 | BSD-3-Clause | https://github.com/scikit-learn/scikit-learn |
| shapely | 2.1.2 | BSD-3-Clause | https://github.com/shapely/shapely |
| striprtf | 0.0.26 | BSD-3-Clause | https://github.com/joshy/striprtf |
| threadpoolctl | 3.6.0 | BSD-3-Clause | https://github.com/joblib/threadpoolctl |
| tifffile | 2026.3.3 | BSD-3-Clause | https://github.com/cgohlke/tifffile |
| xlrd | 2.0.2 | BSD-3-Clause | https://github.com/python-excel/xlrd |

### 1.4 PSF / PSFL Licensed (Python Software Foundation)

| Dependency | Version | License | Source / Repository |
|---|---|---|---|
| aiohappyeyeballs | 2.7.1 | PSF-2.0 | https://github.com/aio-libs/aiohappyeyeballs |
| defusedxml | 0.7.1 | PSF-2.0 | https://github.com/tiran/defusedxml |

### 1.5 LGPL Licensed (Copyleft — Unmodified Library Use)

> **Legal note:** These libraries are used unmodified via pip. LGPL-3.0 does not require
> open-sourcing application code when the library is used as-is through its public API.
> The OpenSSL linking exception on psycopg2 further relaxes redistribution constraints.
> Confirm with counsel before any static linking or modification.

| Dependency | Version | License | Source / Repository | Usage |
|---|---|---|---|---|
| psycopg2-binary | 2.9.12 | LGPL-3.0-or-later WITH OpenSSL-exception | https://github.com/psycopg/psycopg2 | PostgreSQL adapter — unmodified, pip-installed binary wheel |
| psycopg | 3.3.4 | LGPL-3.0-or-later | https://github.com/psycopg/psycopg | PostgreSQL adapter v3 — unmodified |
| psycopg-binary | 3.3.4 | LGPL-3.0-or-later | https://github.com/psycopg/psycopg | PostgreSQL binary extension — unmodified |
| psycopg-pool | 3.3.1 | LGPL-3.0-or-later | https://github.com/psycopg/psycopg | Connection pool — unmodified |
| ldap3 | 2.9.1 | LGPL-3.0-only | https://github.com/cannatag/ldap3 | LDAP auth — **optional**, in requirements-ldap.txt only |



### 1.6 Other Permissive Licenses

| Dependency | Version | License | Source / Repository | Notes |
|---|---|---|---|---|
| certifi | 2026.7.22 | MPL-2.0 | https://github.com/certifi/python-certifi | Mozilla CA bundle; MPL-2.0 file-level copyleft; unmodified use is safe |
| lmdb | 2.3.0 | OLDAP-2.8 | https://github.com/jnwatson/py-lmdb | OpenLDAP Public License — permissive |
| tqdm | 4.70.0 | MPL-2.0 AND MIT | https://github.com/tqdm/tqdm | Dual-licensed; elect MIT |

### 1.7 CodeWiki Build-Stage Dependencies (separate Python 3.12 environment)

> **Why this is a separate section:** the `codewiki` CLI (github.com/FSoft-AI4Code/CodeWiki,
> the engine behind the CodeWiki panel — see `workers/codewiki_worker.py`) requires Python
> >=3.12, while the rest of the platform is pinned to Python 3.11. It is installed via
> `pip install git+https://github.com/FSoft-AI4Code/CodeWiki.git` in a dedicated
> `codewiki_builder` stage in the `Dockerfile` — **not** via `requirements.txt` — and its
> entire self-contained Python installation is copied into the runtime image at
> `/opt/codewiki-python`, isolated from the main app's site-packages. It is ON by default
> (`WITH_CODEWIKI=1`); opt out with `--build-arg WITH_CODEWIKI=0` (or
> `./install.sh --without-codewiki`).
>
> Versions/licenses below were captured directly from the built image's installed package
> metadata (`importlib.metadata`, the same method `compliance/generate-sbom.sh` uses for the
> main app, run against `/opt/codewiki-python/bin/python3.12`) — not hand-transcribed. No
> GPL/LGPL/AGPL-licensed component is present anywhere in this tree.

**The package itself:**

| Dependency | Version | License | Source / Repository |
|---|---|---|---|
| codewiki | 1.0.1 | MIT | https://github.com/FSoft-AI4Code/CodeWiki |

**⚠️ Embedded native component — requires its own note:**

| Dependency | Version | License | Notes |
|---|---|---|---|
| pythonmonkey | 1.3.2 | MIT (Python wrapper) | Embeds a compiled Mozilla **SpiderMonkey** JavaScript engine (MPL-2.0), built from source at pip-install time via its `npm`/`pminit` build hook. Safe to distribute as part of this MIT-licensed project — MPL-2.0 is designed for exactly this "larger work" case and imposes no obligation on the rest of the codebase. The only obligation is narrow: if SpiderMonkey's own source files are ever modified, those modified files must remain MPL-2.0. Do not strip its bundled license text from the wheel. Same treatment as `psycopg2` (Section 1.5) and `certifi` (Section 1.6) above. |

**MIT Licensed:**

| Dependency | Version | License |
|---|---|---|
| annotated-doc | 0.0.5 | MIT |
| annotated-types | 0.8.0 | MIT |
| anyio | 4.15.0 | MIT |
| attrs | 26.1.0 | MIT |
| beartype | 0.22.9 | MIT |
| cachetools | 7.1.8 | MIT |
| cffi | 2.1.1 | MIT-0 |
| exceptiongroup | 1.3.1 | MIT |
| executing | 2.2.1 | MIT |
| fastapi | 0.141.1 | MIT |
| filelock | 3.32.5 | MIT |
| genai-prices | 0.1.6 | MIT |
| httpx-sse | 0.4.3 | MIT |
| jaraco.classes | 3.4.0 | MIT |
| jaraco.context | 6.1.2 | MIT |
| jaraco.functools | 4.6.0 | MIT |
| jeepney | 0.9.0 | MIT |
| jiter | 0.16.0 | MIT |
| jmespath | 1.1.0 | MIT |
| jsonschema | 4.26.0 | MIT |
| jsonschema-specifications | 2025.9.1 | MIT |
| keyring | 25.7.0 | MIT |
| litellm | 1.99.0 | MIT |
| logfire | 4.41.0 | MIT |
| logfire-api | 4.41.0 | MIT |
| loguru | 0.7.3 | MIT |
| mermaid-parser-py | 0.0.4 | MIT |
| mermaid-py | 0.8.4 | MIT |
| more-itertools | 11.1.0 | MIT |
| platformdirs | 4.11.7 | MIT |
| pminit | 1.3.2 | MIT |
| pydantic-ai | 2.31.1 | MIT |
| pydantic-ai-slim | 2.31.1 | MIT |
| pydantic-evals | 2.31.1 | MIT |
| pydantic-graph | 2.31.1 | MIT |
| pydantic_core | 2.46.5 | MIT |
| pythonmonkey | 1.3.2 | MIT (see embedded-component note above) |
| referencing | 0.37.0 | MIT |
| rpds-py | 2026.6.3 | MIT |
| truststore | 0.10.4 | MIT |
| typer | 0.27.2 | MIT |
| typing-inspection | 0.4.4 | MIT |
| urllib3 | 2.7.0 | MIT |
| wcwidth | 0.8.3 | MIT |
| zipp | 4.1.0 | MIT |
| tree-sitter-c | 0.24.2 | MIT |
| tree-sitter-c-sharp | 0.23.5 | MIT |
| tree-sitter-cpp | 0.23.4 | MIT |
| tree-sitter-java | 0.23.5 | MIT |
| tree-sitter-javascript | 0.25.0 | MIT |
| tree-sitter-kotlin | 1.1.0 | MIT |
| tree-sitter-language-pack | 1.16.1 | MIT |
| tree-sitter-php | 0.24.1 | MIT |
| tree-sitter-python | 0.25.0 | MIT |
| tree-sitter-ruby | 0.23.1 | MIT |
| tree-sitter-typescript | 0.23.2 | MIT |

**Apache-2.0 Licensed:**

| Dependency | Version | License |
|---|---|---|
| aiofile | 3.12.3 | Apache-2.0 |
| argcomplete | 3.7.2 | Apache-2.0 |
| boto3 | 1.43.88 | Apache-2.0 |
| botocore | 1.43.88 | Apache-2.0 |
| caio | 0.12.2 | Apache-2.0 |
| coding-agent-wrapper | 0.1.10 | Apache-2.0 |
| distro | 1.9.0 | Apache-2.0 |
| fastmcp-slim | 3.4.7 | Apache-2.0 |
| importlib_metadata | 8.9.0 | Apache-2.0 |
| opentelemetry-api | 1.44.0 | Apache-2.0 |
| opentelemetry-exporter-otlp-proto-common | 1.44.0 | Apache-2.0 |
| opentelemetry-exporter-otlp-proto-http | 1.44.0 | Apache-2.0 |
| opentelemetry-instrumentation | 0.65b0 | Apache-2.0 |
| opentelemetry-instrumentation-httpx | 0.65b0 | Apache-2.0 |
| opentelemetry-proto | 1.44.0 | Apache-2.0 |
| opentelemetry-sdk | 1.44.0 | Apache-2.0 |
| opentelemetry-semantic-conventions | 0.65b0 | Apache-2.0 |
| opentelemetry-util-http | 0.65b0 | Apache-2.0 |
| py-key-value-aio | 0.4.5 | Apache-2.0 |
| regex | 2026.9.3 | Apache-2.0 AND CNRI-Python (both permissive) |
| s3transfer | 0.19.2 | Apache-2.0 |

**BSD Licensed:**

| Dependency | Version | License |
|---|---|---|
| Authlib | 1.8.0 | BSD-3-Clause |
| colorama | 0.4.6 | BSD |
| cryptography | 50.0.1 | Apache-2.0 OR BSD-3-Clause |
| fastuuid | 0.14.0 | BSD |
| fsspec | 2026.7.0 | BSD-3-Clause |
| gitdb | 4.0.12 | BSD |
| GitPython | 3.1.61 | BSD-3-Clause (separate copy from the main app's; see Section 1.3) |
| httpcore2 | 2.12.0 | BSD-3-Clause |
| httpx2 | 2.12.0 | BSD-3-Clause |
| idna | 3.19 | BSD-3-Clause |
| MarkupSafe | 3.0.3 | BSD-3-Clause |
| prompt_toolkit | 3.0.53 | BSD |
| psutil | 7.2.2 | BSD-3-Clause |
| pycparser | 3.0 | BSD-3-Clause |
| Pygments | 2.21.0 | BSD-2-Clause |
| pyperclip | 1.11.0 | BSD |
| SecretStorage | 3.5.0 | BSD-3-Clause |
| smmap | 5.0.3 | BSD-3-Clause |
| sse-starlette | 3.4.10 | BSD-3-Clause |
| starlette | 1.6.0 | BSD-3-Clause |
| uvicorn | 0.52.4 | BSD-3-Clause |
| websockets | 16.1.1 | BSD-3-Clause |
| wrapt | 2.4.0 | BSD-2-Clause |

**ISC / PSF-2.0 Licensed:**

| Dependency | Version | License |
|---|---|---|
| griffelib | 2.2.0 | ISC |
| shellingham | 1.5.4 | ISC |
| typing_extensions | 4.16.0 | PSF-2.0 |

**Other Permissive:**

| Dependency | Version | License | Notes |
|---|---|---|---|
| email-validator | 2.3.0 | Unlicense | Public-domain equivalent |
| pathspec | 1.1.1 | MPL-2.0 | File-level copyleft; unmodified use is safe — same treatment as `certifi`/`tqdm` above |

---

## Section 2 — Node.js / npm Dependencies (ai-ui, desktop)

Source: `compliance/node-components.tsv` (683 packages). Summary by license:

| License | Count | Risk |
|---|---|---|
| MIT | 576 | ✅ Permissive |
| ISC | 50 | ✅ Permissive |
| Apache-2.0 | 18 | ✅ Permissive (NOTICE required) |
| MPL-2.0 | 12 | ⚠️ File-level copyleft — unmodified use safe; confirm with counsel |
| BSD-3-Clause | 11 | ✅ Permissive |
| BSD-2-Clause | 8 | ✅ Permissive |
| 0BSD | 2 | ✅ Permissive |
| MIT-0 | 1 | ✅ Permissive |

### 2.1 Apache-2.0 npm Packages (NOTICE preservation required)

| Dependency | Version | License | Source |
|---|---|---|---|
| @agentclientprotocol/sdk | (see package-lock) | Apache-2.0 | https://github.com/agentclientprotocol/typescript-sdk |
| SheetJS / xlsx | (see package-lock) | Apache-2.0 | https://github.com/SheetJS/sheetjs |
| @nut-tree-fork/nut-js | (see package-lock) | Apache-2.0 | https://github.com/nut-tree-fork/nut.js |
| playwright | (see package-lock) | Apache-2.0 | https://github.com/microsoft/playwright |

### 2.2 MPL-2.0 npm Packages (File-level copyleft — confirm with counsel)

> These 12 packages are under MPL-2.0. Unmodified use in a larger work is permitted.
> If any MPL-2.0 file is modified, the modified file must be released under MPL-2.0.
> Full list available in `compliance/node-components.tsv`.

| Dependency | Version | License | Notes |
|---|---|---|---|
| dompurify | 3.4.14 | MPL-2.0 OR Apache-2.0 | Dual-licensed; elect Apache-2.0 |
| lightningcss | 1.32.0 | MPL-2.0 | |
| lightningcss-android-arm64 | 1.32.0 | MPL-2.0 | Platform binary of the above |
| lightningcss-darwin-arm64 | 1.32.0 | MPL-2.0 | Platform binary of the above |
| lightningcss-darwin-x64 | 1.32.0 | MPL-2.0 | Platform binary of the above |
| lightningcss-freebsd-x64 | 1.32.0 | MPL-2.0 | Platform binary of the above |
| lightningcss-linux-arm-gnueabihf | 1.32.0 | MPL-2.0 | Platform binary of the above |
| lightningcss-linux-arm64-gnu | 1.32.0 | MPL-2.0 | Platform binary of the above |
| lightningcss-linux-arm64-musl | 1.32.0 | MPL-2.0 | Platform binary of the above |
| lightningcss-linux-x64-gnu | 1.32.0 | MPL-2.0 | Platform binary of the above |
| lightningcss-linux-x64-musl | 1.32.0 | MPL-2.0 | Platform binary of the above |
| lightningcss-win32-arm64-msvc | 1.32.0 | MPL-2.0 | Platform binary of the above |
| lightningcss-win32-x64-msvc | 1.32.0 | MPL-2.0 | Platform binary of the above |

Full machine-readable list: `compliance/node-components.tsv`.

## Section 3 — External Runtime Components

> These are not Python or Node packages pulled from a registry — they are
> separate, independently-released software this platform can invoke as a
> subprocess. They are documented here because they are a distributed
> dependency of this platform's optional features, not because they are
> bundled into this repository.

### 3.1 `ainxt` CLI (optional — `AgentStudio` CLI execution mode only)

| Field | Value |
|---|---|
| Component | `ainxt` (binary name), the AiNxt CLI |
| Repository | `ainxt-cli` (companion repository, published separately by NPCI) |
| Upstream project | **Grok Build** (`grok`), by SpaceXAI |
| Upstream repository | https://github.com/xai-org/grok-build |
| Upstream license | Apache License, Version 2.0 — Copyright 2023-2026 SpaceXAI |
| This fork's license | Apache-2.0 (unmodified upstream text, plus NPCI fork-attribution header) |
| Used by | `AgentStudio/backend/app/cli_runtime/*`, `agents/sdlc_cli_engine.py` — only when `ABSTUDIO_CLI_MODE=true` (off by default) |
| Distribution | Not vendored/bundled in this repository. Invoked as an external subprocess; the operator installs it separately from the `ainxt-cli` repository. |

`ainxt` is a rebranded, NPCI-modified fork of xAI/SpaceXAI's **Grok Build** CLI, not
a build of Anthropic's Claude Code. Its command-line surface (`--permission-mode`,
`--output-format streaming-json`, tool names, folder-trust gating, MCP support)
is inherited from the upstream Grok Build project — see `ainxt-cli`'s own `NOTICE`
and `LICENSE` for the complete Apache-2.0 §4(b) record of modifications NPCI made
relative to that upstream. Full attribution, license text, and third-party notices
for `ainxt` itself live in the `ainxt-cli` repository (`LICENSE`, `NOTICE`,
`THIRD-PARTY-NOTICES`) and are not duplicated here; this entry exists so that a
reader of *this* repository's notices is not left unaware that the CLI execution
mode depends on a separately-licensed external component.

**Release note:** `ainxt-cli` carries its own open release-blocking finding
(a GPL-2.0-with-linking-exception library, `libgit2`, statically linked pending
legal sign-off — see that repository's own audit). Enabling `ABSTUDIO_CLI_MODE`
in a public release build inherits that open item until it is resolved upstream
in `ainxt-cli`.
