> **Before you spend time on this:** external pull requests are **not currently
> accepted or triaged**. Contributions are not open yet — see
> [CONTRIBUTING.md](../CONTRIBUTING.md). This template documents the workflow the
> maintaining team follows, and the one external contributions will follow once
> they open.

## What this changes

<!-- One paragraph. What behaviour is different after this PR? -->

## Why

<!-- Link the related issue, e.g. `Closes #123`. -->

## How it was verified

<!-- Commands you ran and what they reported. "Tests pass" on its own is not
     verification — say which tests, and on what platform. -->

```
(commands and output)
```

## Checklist

- [ ] Linked the related issue
- [ ] `ruff format` and `ruff check` clean (Python); lint clean (Node, if touched)
- [ ] Tests added or updated for the behaviour that changed
- [ ] Documentation updated if a setting or user-facing behaviour changed
- [ ] Requested review from the relevant owners in `CODEOWNERS`

### Configurability — required for every change

- [ ] No hardcoded hostnames, URLs, organisation names, credentials, or model identifiers
- [ ] Anything deployment-specific reads from an environment variable or config file,
      **and is documented in `.env.example`**
- [ ] Any new setting has a default that preserves existing behaviour
- [ ] Any new external service is optional — the platform still starts without it

### Privacy and security

- [ ] No secrets, tokens, internal hostnames, or personal data in code, tests,
      fixtures, or logs
- [ ] If this changes what personal data is stored, where it goes, or how long it is
      kept, say so explicitly here — it may need a privacy review
- [ ] New outbound network calls are named below, with their destination

## Notes for reviewers

<!-- Anything hard to see from the diff: a decision and the alternatives you rejected,
     a known limitation, follow-up work. -->
