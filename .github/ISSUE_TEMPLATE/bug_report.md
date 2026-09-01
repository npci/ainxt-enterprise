---
name: Bug report
about: Something does not work the way it is documented
title: ''
labels: bug
assignees: ''
---

> **Before you spend time on this:** external issues are **not currently
> triaged**. AiNxt Enterprise is published under Apache-2.0 as source-available;
> contributions are not open yet, and no commitment is made to review or respond.
> See [CONTRIBUTING.md](../../CONTRIBUTING.md) for the posture and
> [SUPPORT.md](../../SUPPORT.md) for where to look for answers.
>
> **Security vulnerabilities are the exception** and are accepted at any time —
> please use a [private advisory](../../security/advisories/new) rather than this
> form, per [SECURITY.md](../../SECURITY.md).

## What happened

<!-- What you observed. One or two sentences. -->

## What you expected

<!-- What the documentation led you to expect instead. -->

## Steps to reproduce

1.
2.
3.

## Environment

| | |
|---|---|
| Python version (`python --version`) | |
| Node version (`node --version`), if the UI is involved | |
| Operating system | |
| Deployment | <!-- local venv / Docker / other --> |
| `LLM_PROVIDER` | <!-- cloud or local — see docs/PROVIDERS.md --> |
| Model in use | <!-- the resolved model id, if the failure is model-related --> |

## Logs and configuration

Paste the relevant traceback or log lines.

**Redact before posting — issues are public.** Remove API keys, tokens, internal
hostnames, directory paths, real user names, and any prompt or document content that
contains personal data.

```
(traceback / logs)
```

## Anything else

<!-- A minimal reproduction, related issues, when it last worked. -->

---

**Do not use this template for security vulnerabilities or for anything containing
personal data.** Report vulnerabilities privately as described in
[SECURITY.md](../../SECURITY.md).
