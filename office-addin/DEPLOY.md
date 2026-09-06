# AiNxt Office Add-in — deployment guide

Adds an **"Open AiNxt" task pane** inside Outlook, Word, Excel and PowerPoint:
ask questions about the open document or email, run quick actions (draft a
reply, summarise, rewrite), and insert the result back into the document.

This is an **optional component**. The platform runs without it, and nothing
else depends on it.

## How it fits together (read once)

- The task pane is a small web app in `office-addin/src/`, built to
  `office-addin/dist/`.
- **Your own AiNxt gateway serves it.** `gateway.py` mounts
  `office-addin/dist/` at `/office-addin` when that directory exists. There is
  no Microsoft-hosted endpoint and no third party in the path.
- The **manifest** is an XML file telling Office where the add-in lives and how
  to sign in. Your Microsoft 365 administrator uploads it to push the add-in out.
- Sign-in uses **Entra SSO with On-Behalf-Of**: Office issues the add-in a token
  for *your* Entra app; the gateway exchanges it for a Microsoft Graph token
  (`POST /ainxt/v1/api/auth/sso/office`, implemented in `auth/sso.py`).

```
Office client --loads----> https://<your-host>/office-addin/...   (served by YOUR gateway)
Office client --SSO------> your Entra app --assertion--> gateway /auth/sso/office --OBO--> Graph
M365 admin    --uploads manifest--> pushes the task-pane button to your users
```

## Prerequisites

- An AiNxt gateway reachable over **HTTPS** — Office desktop clients silently
  refuse to load a task pane over plain HTTP. `https://localhost` is the only
  exception, and the dev-cert flow below makes that HTTPS too.
- An **Entra (Azure AD) app registration** you control, and a Microsoft 365
  administrator who can grant consent and upload the manifest.
- `SSO_PROVIDER=azure_ad` on the gateway — `auth/sso.py::sso_office` returns
  HTTP 400 otherwise.

## Step 1 — build the task pane

```bash
cd office-addin
npm install
npm run build            # -> office-addin/dist/
```

Restart the gateway so the `/office-addin` mount picks up `dist/`. It logs
*"Serving Office add-in task pane from ..."* when the mount is active. Confirm:

```bash
curl -fsI https://<your-host>/office-addin/src/taskpane/index.html   # expect 200
curl -fsI https://<your-host>/office-addin/icon-32.png               # expect 200
```

## Step 2 — generate your manifests

The four manifests here are **templates**. They ship with placeholders rather
than a hostname, because an Office manifest cannot use relative paths — every
`SourceLocation`, `IconUrl` and `AppDomain` must be an absolute URL, which makes
the manifest deployment-specific by nature.

```bash
AINXT_ADDIN_BASE_URL=https://ainxt.example.com npm run manifests
```

Writes `build/manifest.xml` (Outlook), `build/manifest-word.xml`,
`build/manifest-excel.xml` and `build/manifest-powerpoint.xml`.

| Variable | Required | Default | Notes |
|---|---|---|---|
| `AINXT_ADDIN_BASE_URL` | **yes** | — | Origin only, no path. Must be `https://` (or `https://localhost:<port>`). |
| `AINXT_ADDIN_SUPPORT_URL` | no | base URL | Shown to users in the Office add-in listing. |
| `AINXT_ADDIN_PROVIDER_NAME` | no | `AiNxt` | Publisher name in the same listing. |

`build/` is gitignored — generated manifests belong to one deployment and should
never be committed.

> **Add-in GUIDs.** The `<Id>` in each template identifies *this* add-in, not
> your deployment, so leave them alone: every install of the AiNxt add-in shares
> them. If you fork and change the add-in's behaviour, mint four fresh GUIDs
> (`uuidgen`), one per Office host — Office rejects two different add-ins that
> share an `<Id>`.

## Step 3 — configure the Entra app (administrator, one time)

1. **Expose an API** → set the **Application ID URI** to
   `api://<your-host>/<your-client-id>`.
2. **Add a scope** named `access_as_user`.
3. **Authorized client applications** → pre-authorize the Microsoft Office
   client IDs so users sign in silently instead of getting a popup. Take the
   current list from Microsoft's *"Authorize the Office client application"*
   documentation — it changes, so do not trust a copy pasted into a repo.
4. **API permissions** → add the Graph **delegated** scopes you need, then grant
   admin consent.
5. Edit each generated manifest's `<Resource>` to match the Application ID URI
   from step 1 exactly.

Gateway `.env`:

```bash
SSO_PROVIDER=azure_ad
AZURE_AD_TENANT_ID=<your-tenant-id>
AZURE_AD_CLIENT_ID=<your-client-id>
AZURE_AD_CLIENT_SECRET=<that app's secret value>
# Optional - widen the Graph scopes requested during the OBO exchange:
# AZURE_AD_OBO_SCOPES=openid profile offline_access https://graph.microsoft.com/User.Read
```

> **One hard rule:** the client ID in the manifest `<Resource>` **must equal**
> the gateway's `AZURE_AD_CLIENT_ID`. The OBO exchange validates the add-in's
> token against it, so a mismatch fails every sign-in with `invalid_client`.

## Step 4 — push it to users (administrator)

**Microsoft 365 admin center → Settings → Integrated apps → Upload custom
apps → Upload manifest file.** Upload each manifest and assign users or groups:

| Office application | Manifest |
|---|---|
| Outlook | `build/manifest.xml` |
| Word | `build/manifest-word.xml` |
| Excel | `build/manifest-excel.xml` |
| PowerPoint | `build/manifest-powerpoint.xml` |

Outlook add-ins can also be deployed through **Exchange admin center →
Organization → Add-ins** if your tenant routes them there. Start with a pilot
group; propagation can take up to ~24 hours.

## Local development

```bash
npx office-addin-dev-certs install       # one time - trusted localhost cert
cd office-addin && npm run dev           # vite on https://localhost:3100
AINXT_ADDIN_BASE_URL=https://localhost:3100 npm run manifests
```

Then sideload `build/manifest.xml` into your Office client. Without the dev
certs vite falls back to HTTP and Office refuses to load the pane — the dev
server prints a hint when that happens.

## Verify end to end

1. `curl -fsI https://<your-host>/office-addin/src/taskpane/index.html` → 200.
2. Open Outlook or Word as an assigned user — the ribbon shows **Open AiNxt**.
3. Click it: the pane loads and signs in silently, with no popup.

## Common failures

| Symptom | Cause | Fix |
|---|---|---|
| "Open AiNxt" never appears | Manifest not uploaded, or not assigned | Step 4 |
| Pane blank, or 404 | `dist/` not built, or gateway not restarted | Step 1 |
| Pane shows the AiNxt web UI instead of the task pane | The SPA catch-all answered the request — the `/office-addin` mount is not active | Confirm `office-addin/dist/` exists and the gateway logged *"Serving Office add-in task pane"* |
| Sign-in popup, or `AADSTS65001` / `AADSTS70011` | Application ID URI does not match manifest `<Resource>`, or Office client IDs not pre-authorized | Step 3.1, 3.3, 3.5 |
| `invalid_client`, or OBO fails server-side | Gateway `AZURE_AD_CLIENT_ID`/`SECRET` is not the manifest's app | Step 3 |
| HTTP 400 *"Azure AD SSO is not configured"* | `SSO_PROVIDER` is not `azure_ad` | Step 3 `.env` |
| Graph calls time out | Gateway has no egress to Microsoft | Set `HTTPS_PROXY` if you egress through a proxy |
| Office reports an invalid manifest | A `{{PLACEHOLDER}}` survived, or a manifest was hand-edited | Re-run `npm run manifests`; it fails loudly on unsubstituted placeholders |
