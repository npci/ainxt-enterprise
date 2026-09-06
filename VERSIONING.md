# Versioning and Compatibility Policy

AiNxt Enterprise follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Version scheme

| Component | Meaning |
|---|---|
| **Major** (`X.y.z`) | Breaking changes to stable APIs or deployment contracts |
| **Minor** (`x.Y.z`) | New features, backward-compatible. Deprecation notices issued here |
| **Patch** (`x.y.Z`) | Bug fixes and security patches. No breaking changes |

## Stability tiers

| Surface | Stability | Notes |
|---|---|---|
| OpenAI-compatible endpoint (`/ainxt/v1/api/v1/chat/completions`) | **Stable** (when enabled) | Follows OpenAI Chat Completions schema within a major version |
| REST API routers (`/ainxt/v1/api/*`) | **Unstable in 0.x** | May change between minor releases; stabilises at 1.0.0 |
| Python module interfaces | **Internal** | No stability guarantee; not a public API |
| Docker Compose service names and volume names | **Stable** | Changes announced one minor release in advance |
| Database schema | **Stable** | Migrations are additive in patch/minor; destructive only in major |
| Environment variable names | **Stable** | Renamed vars keep the old name as a deprecated alias for one major version |

## Deprecation process

1. A feature or API is marked deprecated in the release notes and in code comments.
2. It remains functional for at least **one minor release** after deprecation.
3. It is removed in the next **major release** (or the next minor release if it
   is in the unstable tier).
4. A migration guide is published alongside the removal.

## Security patches

Security fixes are backported to the **current stable minor release** as patch
releases. Older minor releases receive security patches only for **critical**
(CVSS ≥ 9.0) vulnerabilities, for a maximum of **6 months** after a new minor
release supersedes them.

## Support boundaries

| Release type | Active support | Security patches |
|---|---|---|
| Current stable minor | Until next minor release | Until next minor release |
| Previous stable minor | 3 months after superseded | 6 months after superseded |
| Community Preview (0.1.x) | Until 0.2.0 | Until 0.2.0 |

## Breaking change policy

A breaking change is any change that requires an operator to modify their
`.env`, `docker-compose.yml`, database, or integration code to maintain
existing behaviour. Breaking changes:

- Are never introduced in patch releases.
- Are announced in the minor release that deprecates the old behaviour.
- Are documented with a migration guide in the major release that removes it.
- Are listed explicitly in `CHANGELOG.md` under a `### Breaking changes` heading.
