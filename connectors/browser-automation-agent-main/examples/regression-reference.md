# Regression Test File Reference

Test files let you save and replay a fixed sequence of browser actions. Attach the file in the side panel and click **Run** — no LLM planning step, just direct execution.

Supported formats: **JSON** and **YAML**.

---

## File Structure

```yaml
test_name: Human-readable name for this test
base_url: https://example.com          # optional — becomes ${base_url} variable
timeout_ms: 15000                      # optional — per-step timeout (default 15s)

variables:                             # optional — reusable values
  username: john.doe
  product: Laptop

steps:
  - action: navigate
    url: ${base_url}/login
  - action: type
    target: "#username"
    value: ${username}
```

---

## All Actions

### `navigate` — Go to a URL
```yaml
- action: navigate
  url: https://example.com/page
```

### `click` — Click any element
```yaml
- action: click
  target: role=button[name="Sign in"]
```

### `type` — Fill a text field
```yaml
- action: type
  target: "#email"
  value: user@example.com
```

### `clear` — Clear a field
```yaml
- action: clear
  target: "#search-input"
```

### `select` — Pick a dropdown option
```yaml
- action: select
  target: "#country"
  value: United States
```

### `check` / `uncheck` — Tick a checkbox
```yaml
- action: check
  target: "#agree-terms"

- action: uncheck
  target: "#newsletter"
```

### `press_key` — Press a keyboard key
```yaml
- action: press_key
  value: Enter

- action: press_key
  target: "#search"
  value: Ctrl+A             # modifier+key combos work too
```

### `hover` — Mouse over an element
```yaml
- action: hover
  target: role=button[name="More options"]
```

### `scroll` — Scroll the page or an element
```yaml
- action: scroll              # scroll window by 500px
  value: "500"

- action: scroll              # scroll to bottom
  value: bottom

- action: scroll              # scroll element into view
  target: "#footer"
  value: into_view
```

### `wait` — Wait for a condition before continuing
```yaml
- action: wait
  condition: visible
  target: "#success-message"

- action: wait
  condition: url_matches:/dashboard
  timeout_ms: 10000

- action: wait
  condition: text:Welcome
  target: ".header"
```

Wait conditions: `visible`, `attached`, `detached`, `enabled`, `text:<substring>`, `url_matches:<regex>`, `network_idle`, `js:<expression>`

### `assert` — Verify something on the page
```yaml
- action: assert
  target: "#flash-message"
  matcher: contains
  expected: "Login successful"
  critical: true            # stop all remaining steps if this fails

- action: assert
  target: role=heading[name="Dashboard"]
  matcher: visible

- action: assert
  target: ".item-count"
  matcher: equals
  expected: "3"
```

Matchers: `equals`, `not_equals`, `contains`, `not_contains`, `matches` (regex), `visible`, `hidden`, `present`, `absent`, `enabled`, `disabled`, `count`, `attribute:NAME`

### `extract` — Capture a value into a variable
```yaml
- action: extract
  target: "#order-id"
  value: order_id           # stored as ${order_id}

- action: extract
  target: "#price"
  attr: data-amount         # read an attribute instead of text
  value: price
```

### `summarize` — AI-summarize page content
```yaml
- action: summarize
  value: page_summary       # stored as ${page_summary}

- action: summarize
  target: "#product-description"   # scope to a specific section
  value: description_summary
```

### `screenshot` — Capture the current viewport
```yaml
- action: screenshot
```

### `request_human` — Pause and ask for manual input
```yaml
- action: request_human
  value: "Please complete the CAPTCHA, then click Resume."
```

---

## Selectors (how to target elements)

Use these in order of preference:

| Selector | Example | When to use |
|---|---|---|
| ARIA role + name | `role=button[name="Submit"]` | Buttons, links, inputs with labels |
| Test ID attribute | `[data-testid="login-btn"]` | When devs add test IDs |
| ID | `#username` | Stable IDs |
| Name attribute | `[name="email"]` | Form fields |
| Visible text | `text="Sign in"` | Links and labels |
| CSS selector | `.submit-btn` | When nothing else works |
| XPath | `xpath=//button[@type='submit']` | Last resort |

---

## Variables and Secrets

### Variables — for reusable non-sensitive values
Define in the file header, reference with `${name}`:
```yaml
variables:
  base_url: https://staging.myapp.com
  username: testuser
  product_id: "42"

steps:
  - action: navigate
    url: ${base_url}/products/${product_id}
  - action: type
    target: "#username"
    value: ${username}
```

Variables can also be **captured at runtime** using `extract` or `summarize` and used in later steps:
```yaml
- action: extract
  target: "#confirmation-number"
  value: conf_num

- action: assert
  target: ".receipt .number"
  matcher: equals
  expected: ${conf_num}
```

### Secrets — for passwords and API keys
Store secrets in the Settings panel (Secrets JSON), never in the file:
```json
{ "password": "MyP@ssw0rd", "api_key": "sk-..." }
```

Reference them with `${secrets.KEY}`:
```yaml
- action: type
  target: "#password"
  value: ${secrets.password}
```

Secrets are **never shown in logs or output** — they are redacted as `***`.

---

## Real-World Examples

### Login + Verify Dashboard
```yaml
test_name: Login flow
base_url: https://myapp.com
timeout_ms: 15000

steps:
  - action: navigate
    url: ${base_url}/login

  - action: type
    target: "#email"
    value: admin@example.com

  - action: type
    target: "#password"
    value: ${secrets.password}

  - action: click
    target: role=button[name="Sign in"]

  - action: wait
    condition: url_matches:/dashboard

  - action: assert
    target: role=heading[name="Dashboard"]
    matcher: visible
    critical: true

  - action: screenshot
```

---

### Registration Form
```yaml
test_name: New user registration
base_url: https://myapp.com

variables:
  first_name: Jane
  last_name: Doe
  email: jane.doe+test@example.com

steps:
  - action: navigate
    url: ${base_url}/register

  - action: type
    target: "[name='firstName']"
    value: ${first_name}

  - action: type
    target: "[name='lastName']"
    value: ${last_name}

  - action: type
    target: "[name='email']"
    value: ${email}

  - action: type
    target: "[name='password']"
    value: ${secrets.password}

  - action: select
    target: "#country"
    value: United States

  - action: check
    target: "#terms"

  - action: click
    target: role=button[name="Create account"]

  - action: wait
    condition: text:Welcome
    target: "h1"
    timeout_ms: 20000

  - action: assert
    target: "h1"
    matcher: contains
    expected: Welcome
    critical: true
```

---

### Search and Verify Results
```yaml
test_name: Product search
base_url: https://shop.example.com

variables:
  search_term: wireless headphones

steps:
  - action: navigate
    url: ${base_url}

  - action: type
    target: role=searchbox
    value: ${search_term}

  - action: press_key
    value: Enter

  - action: wait
    condition: url_matches:/search

  - action: assert
    target: ".results-count"
    matcher: present

  - action: extract
    target: ".results-count"
    value: result_count

  - action: assert
    target: ".product-card"
    matcher: count
    expected: "10"            # expect first page to have 10 items
```

---

### E-Commerce Checkout (with human gate)
```yaml
test_name: Checkout flow — stops before payment
base_url: https://shop.example.com

steps:
  - action: navigate
    url: ${base_url}/cart

  - action: assert
    target: ".cart-item"
    matcher: present
    critical: true

  - action: click
    target: role=button[name="Proceed to checkout"]

  - action: wait
    condition: url_matches:/checkout

  - action: type
    target: "[name='address']"
    value: 123 Test Street

  - action: select
    target: "#state"
    value: California

  - action: type
    target: "[name='zip']"
    value: "90210"

  - action: screenshot

  - action: request_human
    value: "Shipping filled. Review before clicking 'Continue to payment'."
```

---

## Tips

- Mark critical steps with `critical: true` — the run stops immediately if they fail instead of continuing through broken steps.
- Use `extract` to capture dynamic values (order IDs, confirmation numbers) and re-use them in later assertions.
- Use `screenshot` before and after key actions so you can visually diff runs.
- Set `timeout_ms` higher for slow pages or heavy SPAs (e.g. `timeout_ms: 30000`).
- Use `wait` with `url_matches` after navigation actions on SPAs that don't trigger a full page load.
- Keep secrets out of the file entirely — configure them once in Settings.
