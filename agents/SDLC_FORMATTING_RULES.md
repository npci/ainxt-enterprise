# SDLC Pipeline — Jira & Confluence Formatting Rulebook

Any time you touch `sdlc_pipeline.py`, `_make_confluence_md_*`, or anything that
formats LLM output into Jira comments or Confluence pages, follow every rule here.

---

## Rule 1 — Always use `_s()` on every LLM list item before appending to `lines`

The LLM can return `string`, `dict`, or **nested list** for any field marked as a list.
Never append raw items from LLM output to a `lines` list that will be `"\n".join()`-ed.

**Wrong:**
```python
for step in plan:
    lines.append(f"{i}. {step}")       # crashes if step is a dict or list
```

**Right:**
```python
for i, step in enumerate(plan, 1):
    lines.append(f"{i}. {_s(step)}")   # always safe
```

Applies to: `implementation_plan`, `files_to_change`, `new_files_needed`, `risks`,
`dependencies`, `open_questions`, `affected_components`, `tests_to_add`,
`verification_steps`, `triage_steps`, `hypotheses`, `sub_tasks`, `tasks`.

---

## Rule 2 — Always use `_s()` on scalar LLM fields before putting them in `lines +=`

Scalar fields (`solution_approach`, `data_model_changes`, `api_changes`,
`testing_strategy`, `rollback_strategy`, `root_cause`, `fix_description`, etc.)
can come back as a list from the LLM instead of a string.

**Wrong:**
```python
arch = des.get("solution_approach", "")
lines += ["## Approach", "", arch, ""]    # arch could be a list → crash
```

**Right:**
```python
arch = _s(des.get("solution_approach") or "")
lines += ["## Approach", "", arch, ""]    # always a string now
```

---

## Rule 3 — `_s()` must handle str, dict, AND list

Current implementation (keep it this way):

```python
def _s(item) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, list):
        return "; ".join(_s(x) for x in item if x is not None and x != "")
    if isinstance(item, dict):
        return (item.get("name") or item.get("component") or item.get("file")
                or item.get("path") or item.get("step") or item.get("description")
                or item.get("text") or item.get("value") or item.get("content")
                or str(item))
    return str(item)
```

If you add new dict keys the LLM returns, add them to the dict fallback chain.

---

## Rule 4 — Never use `str(x)` as a substitute for `_s(x)`

`str({"file": "foo.py"})` gives `"{'file': 'foo.py'}"` — ugly and useless.
`_s({"file": "foo.py"})` gives `"foo.py"` — correct.

---

## Rule 5 — Jira webhook: silently drop non-`issue_created` events

Never log a message for ignored webhook events. Jira fires `comment_created`,
`issue_updated`, `jira:issue_updated`, etc. constantly. Just return `{"accepted": True}`.

**Wrong:**
```python
if event != "jira:issue_created":
    logger.info(f"ignoring event '{event}'")   # floods logs
    return {...}
```

**Right:**
```python
if event and event != "jira:issue_created":
    return {"accepted": True}   # silent drop
```

---

## Rule 6 — Log the Jira event AFTER the guard, not before

**Wrong:**
```python
logger.info(f"event={event} key={key}")   # logs even for comment_created
if event != "jira:issue_created":
    return {"accepted": True}
```

**Right:**
```python
if event and event != "jira:issue_created":
    return {"accepted": True}
logger.info(f"event={event} key={key}")   # only logs real triggers
```

---

## Rule 7 — GitHub PR Review: no inline comments with `line=` anchors

The GitHub PR Review API requires `line` to be an exact position in the diff.
The LLM cannot reliably produce valid diff positions → 422 errors every time.

**Never do this:**
```python
comments=[{"path": f, "line": 1, "body": msg}]
```

**Always post as review body only:**
```python
github_create_pr_review(repo, pr_number, body=review_body, event=event, comments=None)
```

If `github_create_pr_review` returns `"[Error..."`, fall back to `github_comment_on_pr`.

---

## Rule 8 — `github_create_pr` must be idempotent (handle 422)

GitHub returns 422 when a PR already exists for the same head branch.
On 422, call `_find_existing_pr(repo, head)` and return the existing PR URL.
Never let the pipeline fail just because the PR was already created.

---

## Rule 9 — Check for `"[Error..."` strings, don't rely on exceptions

GitHub tool functions return `"[Error: ...]"` strings instead of raising.
Always check:
```python
result = github_create_pr_review(...)
if result.startswith("[Error"):
    fallback = github_comment_on_pr(repo, pr_number, body)
```

---

## Rule 10 — SDLC run must be pre-created in the webhook handler

Never create the SDLC run only inside the RQ worker job.
Pre-create it in the webhook handler immediately so the UI shows the run at once,
then pass `_run_id` into the job payload.
Add an inline daemon-thread fallback with a Redis lock for when workers are down.
