# Contributing to AiNxt Enterprise


Thank you for your interest in contributing! This document explains how to get
involved, what we expect from contributors, and how the review process works.

---

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Ways to Contribute](#ways-to-contribute)
- [Getting Started](#getting-started)
- [Development Workflow](#development-workflow)
- [Commit Guidelines](#commit-guidelines)
- [Developer Certificate of Origin (DCO)](#developer-certificate-of-origin-dco)
- [Pull Request Process](#pull-request-process)
- [Coding Standards](#coding-standards)
- [Testing](#testing)
- [Security Vulnerabilities](#security-vulnerabilities)
- [License](#license)

---

## Code of Conduct

By participating in this project you agree to abide by our
[Code of Conduct](CODE_OF_CONDUCT.md). Please read it before contributing.

---

## Ways to Contribute

_When contributions open, these will be the ways to take part. Until then,
external issues and pull requests are **not currently accepted or triaged**, and
no commitment is made to review or respond to them — see
[Contributing in the README](README.md#contributing). Security vulnerabilities are
the exception and may be reported privately at any time; see
[SECURITY.md](SECURITY.md)._

- **Bug reports** — open an issue using the [bug report template](.github/ISSUE_TEMPLATE/bug_report.md).
- **Feature requests** — open an issue using the [feature request template](.github/ISSUE_TEMPLATE/feature_request.md).
- **Documentation** — fix typos, improve clarity, add examples.
- **Code** — fix bugs, implement features, improve performance.
- **Reviews** — review open pull requests and provide constructive feedback.

---

## Getting Started

### Prerequisites

- Python 3.10 – 3.12 (not 3.13+)
- Node.js 18+
- Docker & Docker Compose (for full-stack local dev)
- PostgreSQL 15+ with pgvector extension

### Local Setup

```bash
# 1. Fork and clone
git clone https://github.com/<your-fork>/ainxt-platform.git
cd ainxt-platform

# 2. Set up environment
cp .env.example .env
# Edit .env — at minimum set DATABASE_URL, JWT_SECRET, and one LLM provider key

# 3. Python backend
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
pip install -e ".[dev]"           # installs dev extras (pytest, ruff, etc.)

# 4. Frontend
cd ai-ui && npm install && npm run dev

# 5. Run backend
uvicorn gateway:app --host 0.0.0.0 --port 8000 --reload
```

For the full stack (PostgreSQL, Redis, pgvector, all services), use:

```bash
docker compose up -d
```

---

## Development Workflow

1. **Create a branch** from `main`:
   ```bash
   git checkout -b feat/my-feature   # or fix/my-bug
   ```

2. **Make your changes** — keep commits focused and atomic.

3. **Run tests and linting** before pushing (see [Testing](#testing)).

4. **Push and open a PR** against `main`.

5. **Address review feedback** — maintainers may request changes.

6. **Merge** — a maintainer will merge once approved.

---

## Commit Guidelines

We follow [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <short summary>

[optional body]

[optional footer — DCO sign-off goes here]
```

**Types:** `feat`, `fix`, `docs`, `style`, `refactor`, `test`, `chore`, `ci`

**Examples:**
```
feat(llm-proxy): add circuit-breaker retry with exponential backoff
fix(guardrails): correct PII regex for Aadhaar format
docs(readme): update quick-start for Docker Compose
```

Keep the summary line under 72 characters. Use the body to explain *why*,
not *what* (the diff shows what).

---

## Developer Certificate of Origin (DCO)

This project uses the **Developer Certificate of Origin (DCO)** instead of a
Contributor License Agreement (CLA). By signing off your commits you certify
that you have the right to submit the contribution under the Apache-2.0 license.

**Sign off every commit** with `-s`:

```bash
git commit -s -m "feat(agents): add tool-call retry logic"
```

This appends:
```
Signed-off-by: Your Name <your.email@example.com>
```

The full DCO text is in the [DCO](DCO) file at the root of this repository.

> **Note:** PRs without a DCO sign-off on every commit will not be merged.
> If you forgot, you can amend: `git commit --amend -s` (for the last commit)
> or `git rebase --signoff HEAD~N` (for the last N commits).

---

## Pull Request Process

1. Fill in the [PR template](.github/PULL_REQUEST_TEMPLATE.md) completely.
2. Link the related issue (e.g., `Closes #123`).
3. Ensure all CI checks pass (lint, tests, gitleaks secret scan).
4. Request a review from the relevant code owners (see `CODEOWNERS` file in the repository root).
5. Do not merge your own PR — at least one maintainer approval is required.

### PR Size Guidelines

| Size | Lines changed | Guidance |
|------|:---:|---------|
| Small | < 200 | Preferred — fast to review |
| Medium | 200–500 | Fine — include a clear description |
| Large | > 500 | Split if possible; add a detailed description |

---

## Coding Standards

### Python

- **Formatter:** `ruff format` (Black-compatible)
- **Linter:** `ruff check`
- **Type hints:** required for all public functions and class methods
- **Docstrings:** Google style for public APIs

```bash
ruff format .
ruff check . --fix
```

### TypeScript / JavaScript

- **Formatter:** Prettier (config in `ai-ui/.prettierrc`)
- **Linter:** ESLint (config in `ai-ui/.eslintrc`)

```bash
cd ai-ui && npm run lint && npm run format
```

### General

- No hardcoded secrets, credentials, or internal hostnames — use env vars.
- No `verify=False` on TLS connections.
- No wildcard CORS (`allow_origins=["*"]`) in production code.
- Keep `.env` out of commits — it is in `.gitignore`.

---

## Testing

### Python

```bash
# Run all tests
pytest tests/ -v

# Run with coverage
pytest tests/ --cov=. --cov-report=term-missing
```

New features must include tests. Bug fixes should include a regression test.

### Frontend

```bash
cd ai-ui && npm test
```

### CI

All PRs run the full CI pipeline (`.github/workflows/ci.yml`):
- Python lint (`ruff`) + tests (`pytest`)
- Node build + lint
- Secret scan (`gitleaks`)
- CVE scan stub (`pip-audit` / `npm audit`)

---

## Security Vulnerabilities

**Do not open a public issue for security vulnerabilities.**
Please follow the process in [SECURITY.md](SECURITY.md).

---

## License

By contributing to AiNxt Platform, you agree that your contributions will be
licensed under the [Apache License, Version 2.0](LICENSE).

You retain copyright of your contributions. The DCO sign-off certifies that
you have the right to submit the work under this license.

## Third-party code and dependency additions

If you copy, port or adapt code from another project, three things must be true
before the PR is mergeable:

1. **The licence permits it and is compatible with Apache-2.0.** Permissive
   (MIT/BSD/Apache-2.0/ISC) is fine; copyleft (GPL/LGPL/AGPL) is not — note the
   platform's PDF backend is selectable precisely because one dependency was AGPL.
2. **The origin is recorded** in `NOTICE` / `compliance/third-party-notices.md`:
   upstream project, source URL, licence, and any local modification.
3. **The upstream copyright headers stay intact.**

Adding a dependency (pip or npm): prefer one already in the tree; state in the PR why
it is needed and what it pulls in transitively; and keep any new package **optional**
where the feature it serves is optional, so the platform still starts without it.
