# Phase 3 Design Contract
**Author:** p3_creative | **Date:** 2026-04-27 | **Status:** LOCKED

This document is the single source of truth for all UI decisions in Phase 3.
Builder reads this before writing a single line of HTML, CSS, or JS.
UX agent audits against this after builder finishes.
No deviations without an explicit note explaining the reason.

---

## 1. Design Tokens (verbatim from Prospeqt design system)

Source: `tools/spam-checker/static/style.css` - canonical light theme.
Copy this block verbatim into `static/main.css`. Do not rename variables. Do not add new colors.

```css
:root {
  /* Backgrounds */
  --bg: #f5f5f7;          /* page background */
  --bg-el: #ffffff;       /* surface / card */
  --bg-hov: #f0f0f2;      /* hover state */
  --bg-sel: #e8eeff;      /* selected state */
  --bg-code: #fafafc;     /* code/mono blocks */

  /* Borders */
  --bd: #e0e0e4;
  --bd-s: #d0d0d4;        /* stronger border */

  /* Text */
  --tx1: #1a1a1a;         /* primary */
  --tx2: #6b6b6b;         /* secondary */
  --tx3: #8a8a8e;         /* tertiary / labels */

  /* Brand blue */
  --blue: #2756f7;
  --blue-h: #1679fa;      /* hover */
  --blue-d: #0a61d1;      /* pressed */
  --blue-bg: rgba(39, 86, 247, 0.08);
  --blue-bd: rgba(39, 86, 247, 0.2);

  /* Semantic */
  --green: #1a8a3e;
  --green-bg: rgba(26, 138, 62, 0.08);
  --amber: #b87a00;
  --amber-bg: rgba(184, 122, 0, 0.08);
  --red: #c33939;
  --red-bg: rgba(195, 57, 57, 0.08);

  /* Shadows */
  --sh-sm: 0 1px 2px rgba(0, 0, 0, 0.04);
  --sh: 0 0.6px 0.6px -1.25px rgba(0, 0, 0, 0.06),
        0 2.3px 2.3px -2.5px rgba(0, 0, 0, 0.05),
        0 10px 10px -3.75px rgba(0, 0, 0, 0.03);
  --sh-md: 0 4px 12px rgba(0, 0, 0, 0.08);
  --sh-blue: 0 2px 8px rgba(22, 121, 250, 0.2);

  /* Radii */
  --r-sm: 6px;
  --r: 10px;
  --r-lg: 12px;
}
```

### Font loading (Google Fonts CDN - no local files)

```html
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Space+Grotesk:wght@500;600;700&family=Space+Mono:wght@400;700&display=swap" rel="stylesheet">
```

### Font roles (strict)
- **Inter** - all body text, buttons, labels, UI copy
- **Space Grotesk** - headings (h1, h2, logo), section titles only
- **Space Mono** - ALL monospace output: spintax body, code blocks, stat badges, word counts, cost display

### Button gradient (canonical)
```css
background: linear-gradient(180deg, var(--blue-h) -23%, var(--blue-d) 100%);
box-shadow: var(--sh-blue);
```

---

## 2. Page Layout - index.html (logged in)

### Shell structure

```
<body>
  <div class="topbar">
    <div class="topbar-inner">        <!-- max-width: 920px, centered -->
      .logo                           <!-- left: "P" mark + "Spintax Generator" -->
      .topbar-right                   <!-- right: .admin-badge + .tool-switcher -->
    </div>
  </div>

  <div class="container">            <!-- max-width: 920px, padding: 32px 24px 80px -->
    <section class="input-section">  <!-- INPUT CARD -->
    <section class="progress-section" id="progress" data-state="idle" style="display:none">  <!-- PROGRESS -->
    <section class="output-section" id="output" style="display:none">  <!-- OUTPUT CARD -->
    <div class="footer">
  </div>
</body>
```

### Topbar

Left side:
```html
<a href="/" class="logo">
  <span class="logo-mark">P</span>
  Spintax Generator
</a>
```
- `.logo-mark`: 22x22px, border-radius 6px, gradient background, white "P", font-weight 700, 13px
- `.logo`: Space Grotesk 700, 17px, letter-spacing -0.03em, color var(--tx1)

Right side (flex, gap 8px):
```html
<div class="topbar-right">
  <span class="admin-badge">Team</span>
  <div class="tool-switcher">...</div>   <!-- standard Prospeqt tool switcher from spam-checker -->
</div>
```
- `.admin-badge`: Space Mono, 11px, color var(--tx3), border 1px solid var(--bd), border-radius var(--r-sm), padding 3px 10px. Shows "Team" - no logout button in topbar (not needed for team tool).

Tool switcher: copy the component verbatim from spam-checker index.html + style.css. Spintax Generator is the active item.

### Input section

```html
<section class="input-section">
  <label for="email-body">Plain email body</label>
  <textarea id="email-body" rows="12"
    placeholder="Paste your plain email here. No spintax yet - just the original copy with {{variables}} as-is."></textarea>

  <div class="input-meta">
    <div class="platform-control">
      <span class="platform-label">Platform</span>
      <div class="segmented" role="group" aria-label="Target platform">
        <button type="button" class="seg-btn seg-btn--active" data-platform="instantly" aria-pressed="true">Instantly</button>
        <button type="button" class="seg-btn" data-platform="emailbison" aria-pressed="false">EmailBison</button>
      </div>
    </div>
    <span class="word-count" id="word-count" aria-live="polite">0 words</span>
  </div>

  <div class="input-actions">
    <button id="generate-btn" class="btn btn--full" onclick="startGeneration()">
      Generate spintax
    </button>
    <div class="cap-display" id="cap-display"><!-- injected by JS after first poll --></div>
  </div>

  <div id="input-error" class="field-error" role="alert" aria-live="assertive" style="display:none">
    Please paste an email body before generating.
  </div>
</section>
```

**Textarea:**
- min-height: 240px (desktop), 180px (mobile)
- font-family: Inter (NOT monospace - input is plain text)
- font-size: 14px, line-height: 1.65
- resize: vertical
- focus ring: `box-shadow: 0 0 0 3px var(--blue-bg); border-color: var(--blue);`
- error state: `border-color: var(--red); box-shadow: 0 0 0 3px var(--red-bg);`

**Segmented control (platform):**
- Container: `display: inline-flex; background: var(--bg); border: 1px solid var(--bd); border-radius: var(--r-sm); padding: 2px; gap: 2px;`
- Each button: Inter 13px 500, padding 6px 16px, border-radius 5px, border: none
- Active: `background: var(--bg-el); color: var(--tx1); box-shadow: var(--sh-sm); font-weight: 600;`
- Inactive: `background: transparent; color: var(--tx2);`
- Hover inactive: `background: var(--bg-hov); color: var(--tx1);`

Rationale for segmented over radio buttons: segmented control is a single visual unit with immediate toggle feedback. Radios in a form look like a settings panel. This is a 2-option toggle - segmented is the right pattern.

**Generate button:**
- `.btn--full`: `width: 100%; justify-content: center; min-height: 44px;` (full-width, easier touch target)
- Disabled during generation: `opacity: 0.5; cursor: not-allowed;`
- Loading state text: innerHTML set to `<span class="spinner-white"></span>Generating...`

**Cap display** (injected by JS once first status response arrives):
```html
<span class="cap-display-text">Daily cap: $4.20 / $20.00 used</span>
```
- Space Mono 11px, color var(--tx3), displayed inline under the button

**Field error:**
- `.field-error`: color var(--red), font-size 12px, margin-top 8px, display flex, gap 6px
- Shown when Generate clicked with empty textarea

### Progress section

```html
<section class="progress-section" id="progress" data-state="idle" style="display:none"
         aria-label="Generation progress" aria-live="polite">
  <div class="progress-card">
    <div class="progress-list" id="progress-list">
      <!-- phases injected by JS -->
    </div>
    <div class="progress-meta" id="progress-meta">
      <!-- elapsed time, e.g. "43s elapsed" - injected by JS -->
    </div>
  </div>
</section>
```

**Progress card:**
- background var(--bg-el), border 1px solid var(--bd), border-radius var(--r-lg), padding 20px 24px, box-shadow var(--sh)
- margin-top: 24px

**Progress list item structure (per phase):**
```html
<div class="phase-row phase-row--active" data-phase="drafting">
  <span class="phase-icon"><span class="spinner"></span></span>
  <span class="phase-label">Drafting...</span>
  <span class="phase-duration"></span>
</div>
```

Phase row states:
- `--pending`: icon is gray circle (opacity 0.3), label color var(--tx3)
- `--active`: icon is blue spinner, label color var(--tx1), font-weight 500
- `--done`: icon is green checkmark SVG, label color var(--tx2), duration shown in var(--tx3) Space Mono 12px
- `--failed`: icon is red X SVG, label color var(--red)

**Blue spinner:**
```css
.spinner {
  display: inline-block;
  width: 14px; height: 14px;
  border: 2px solid var(--blue-bg);
  border-top-color: var(--blue);
  border-radius: 50%;
  animation: spin 0.7s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) {
  .spinner { animation: none; border-top-color: var(--blue); }
}
```

**White spinner (for button loading state):**
```css
.spinner-white {
  display: inline-block;
  width: 13px; height: 13px;
  border: 2px solid rgba(255,255,255,0.3);
  border-top-color: #fff;
  border-radius: 50%;
  animation: spin 0.7s linear infinite;
  margin-right: 8px;
  vertical-align: -2px;
}
```

**ETA display:** After 30 seconds of polling with no terminal state, inject below progress list:
```html
<div class="progress-meta">
  <span class="mono-stat">43s elapsed - o3 usually takes 60-170s</span>
</div>
```
`.mono-stat`: Space Mono 11px, color var(--tx3)

### Output section

```html
<section class="output-section" id="output" style="display:none">
  <div class="output-header">
    <div class="output-badges" id="output-badges">
      <!-- lint badge + qa badge + cost pill - injected by JS -->
    </div>
    <div class="mode-toggle" role="group" aria-label="Output display mode">
      <button type="button" class="seg-btn seg-btn--active" data-mode="raw"
              aria-pressed="true" id="mode-raw" onclick="setMode('raw')">Raw spintax</button>
      <button type="button" class="seg-btn" data-mode="preview"
              aria-pressed="false" id="mode-preview" onclick="setMode('preview')">Preview variant</button>
    </div>
  </div>

  <!-- Raw mode container -->
  <div id="raw-container" class="output-body">
    <pre id="raw-output" class="spintax-output"></pre>
    <!-- spintax blocks highlighted by JS - see Section 6 -->
  </div>

  <!-- Preview mode container (hidden by default) -->
  <div id="preview-container" class="output-body" style="display:none">
    <div id="preview-output" class="preview-output"></div>
  </div>

  <div class="output-actions">
    <button class="btn btn-ghost" id="copy-btn" onclick="copyRaw()"
            aria-label="Copy raw spintax to clipboard">
      Copy spintax
    </button>
    <button class="btn btn-ghost" id="download-btn" onclick="downloadRaw()"
            aria-label="Download raw spintax as .txt file">
      Download .txt
    </button>
    <button class="btn btn-ghost" id="randomize-btn" onclick="randomizePreview()"
            aria-label="Re-roll preview variant"
            style="display:none" id="randomize-btn">
      Re-roll preview
    </button>
    <span class="word-count" id="output-word-count">0 words</span>
  </div>
</section>
```

**Output badges injected by JS (done state):**
```html
<span class="badge badge-green">Lint PASS</span>
<span class="badge badge-green">QA PASS</span>
<span class="badge badge-info mono">$0.22 - 4 calls - 103s</span>
```
Or for QA fail:
```html
<span class="badge badge-green">Lint PASS</span>
<span class="badge badge-amber">QA issues</span>
<span class="badge badge-info mono">$0.31 - 6 calls - 145s</span>
```

**Badge styles:**
```css
.badge {
  display: inline-flex; align-items: center;
  padding: 3px 10px; border-radius: 100px;
  font-size: 12px; font-weight: 600; font-family: Inter;
}
.badge-green  { background: var(--green-bg);  color: var(--green);  border: 1px solid rgba(26,138,62,0.2); }
.badge-amber  { background: var(--amber-bg);  color: var(--amber);  border: 1px solid rgba(184,122,0,0.2); }
.badge-red    { background: var(--red-bg);    color: var(--red);    border: 1px solid rgba(195,57,57,0.2); }
.badge-info   { background: var(--bg-hov);    color: var(--tx2);    border: 1px solid var(--bd); }
.badge.mono   { font-family: 'Space Mono', monospace; font-size: 11px; font-weight: 400; }
```

**Output header layout:**
- `display: flex; align-items: center; justify-content: space-between; margin-bottom: 14px;`
- Left: `.output-badges` (flex, gap 6px)
- Right: `.mode-toggle` (same segmented control pattern as platform selector)

**Spintax output pre block:**
```css
.spintax-output {
  font-family: 'Space Mono', monospace;
  font-size: 13px;
  line-height: 1.75;
  color: var(--tx1);
  background: var(--bg-code);
  border: 1px solid var(--bd);
  border-radius: var(--r-lg);
  padding: 20px;
  white-space: pre-wrap;
  word-wrap: break-word;
  margin: 0;
  min-height: 160px;
}
```

**Preview output div:**
```css
.preview-output {
  font-family: 'Inter', sans-serif;
  font-size: 14px;
  line-height: 1.75;
  color: var(--tx1);
  background: var(--bg-el);
  border: 1px solid var(--bd);
  border-radius: var(--r-lg);
  padding: 20px;
  white-space: pre-wrap;
  word-wrap: break-word;
  min-height: 160px;
}
```

**Output actions:**
- `display: flex; align-items: center; gap: 10px; margin-top: 14px; flex-wrap: wrap;`
- `.word-count` at the right (margin-left: auto)
- Randomize button only visible when mode = preview

---

## 3. Page Layout - login.html

Simple centered card. No topbar needed - user is not yet authenticated.

```
<body>                               <!-- bg: var(--bg) -->
  <div class="login-shell">         <!-- display: flex; align-items: center; justify-content: center; min-height: 100vh; -->
    <div class="login-card">        <!-- bg: var(--bg-el); border: 1px solid var(--bd); border-radius: var(--r-lg); padding: 40px 36px; width: 360px; box-shadow: var(--sh-md) -->
      <div class="login-logo">      <!-- centered logo -->
        <span class="logo-mark">P</span>
        Spintax Generator
      </div>
      <p class="login-subtitle">Team access only. Enter your password.</p>
      <form id="login-form" onsubmit="submitLogin(event)" novalidate>
        <label for="password">Password</label>
        <input type="password" id="password" name="password" autocomplete="current-password"
               placeholder="Team password" required>
        <div id="login-error" class="field-error" role="alert" aria-live="assertive" style="display:none">
          Wrong password. Try again.
        </div>
        <button type="submit" id="login-btn" class="btn btn--full" style="margin-top: 20px">
          Sign in
        </button>
      </form>
    </div>
  </div>
</body>
```

**Login card dimensions:**
- Desktop: `width: 360px; max-width: calc(100vw - 48px);`
- Mobile: card fills width with 24px side padding

**Login logo:**
- `display: flex; align-items: center; justify-content: center; gap: 8px;`
- Same `.logo-mark` and Space Grotesk type as topbar, but centered
- `margin-bottom: 8px;`

**Login subtitle:**
- Inter 14px, color var(--tx2), text-align center, margin-bottom 24px

**Password input:**
```css
input[type="password"] {
  width: 100%;
  height: 44px;
  background: var(--bg-el);
  border: 1px solid var(--bd);
  border-radius: var(--r-lg);
  color: var(--tx1);
  font-family: 'Inter', sans-serif;
  font-size: 14px;
  padding: 0 16px;
  transition: border-color 0.15s, box-shadow 0.15s;
  box-shadow: var(--sh-sm);
}
input[type="password"]:focus {
  outline: none;
  border-color: var(--blue);
  box-shadow: 0 0 0 3px var(--blue-bg);
}
input[type="password"].error {
  border-color: var(--red);
  box-shadow: 0 0 0 3px var(--red-bg);
}
```

**No "forgot password" or "register" links.** Team-only, shared password.

---

## 4. State-Driven UI Spec

Every state change is driven by `data-state` attribute updates on `#progress`.
Playwright asserts against these attributes - not just visual appearance.

### State machine transitions for the UI

```
idle       - page load, no job running
queued     - POST /api/spintax returned job_id, awaiting first status poll
drafting   - job.status == "drafting"
linting    - job.status == "linting"
iterating  - job.status == "iterating"
qa         - job.status == "qa"
done       - job.status == "done" (qa.passed true or false)
failed     - job.status == "failed"
```

`data-state` attribute lives on `<section id="progress">` AND the root `<div id="app">` on `<body>`.
Builder sets BOTH so Playwright can assert from either selector.

Example: `document.getElementById('progress').dataset.state = 'drafting';`
Also: `document.body.dataset.state = 'drafting';`

### Per-state UI matrix

| State | #progress visibility | Generate btn | Textarea | Progress list | Output section |
|---|---|---|---|---|---|
| `idle` | hidden | enabled, normal | enabled | n/a | hidden |
| `queued` | visible | disabled + spinner | disabled | "Queued..." gray dot | hidden |
| `drafting` | visible | disabled + spinner | disabled | Drafting active | hidden |
| `linting` | visible | disabled + spinner | disabled | Linting active | hidden |
| `iterating` | visible | disabled + spinner | disabled | Iterating active (with count) | hidden |
| `qa` | visible | disabled + spinner | disabled | QA active | hidden |
| `done` | visible (phases frozen as done) | enabled, normal | enabled | All done | visible |
| `failed` | visible (last phase marked failed) | enabled, normal | enabled | Last phase red | error card visible |

### Progress list phases rendered per state

Phases rendered once on job start. Icons update as state advances.

```
Phase        Label shown            When active
---------    --------------------   ---------------------------
drafting     Drafting               state == drafting
linting      Linting                state == linting OR iterating
iterating    Iteration N            state == iterating (N from progress.iteration_count)
qa           Running QA             state == qa
```

For `iterating` state, append the iteration count to the label:
- First iteration: "Iteration 1 - fixing blocks"
- Second: "Iteration 2 - fixing blocks"
- Use `job.progress.iteration_count` if present in the poll response (phase 2 provides this via `progress` field in `JobStatusResponse`)

### Detailed state displays

**`queued`:**
```html
<div class="phase-row phase-row--active" data-phase="queued">
  <span class="phase-icon"><span class="spinner"></span></span>
  <span class="phase-label">Queued...</span>
</div>
```

**`drafting`:**
```html
<div class="phase-row phase-row--done" data-phase="queued">...</div>
<div class="phase-row phase-row--active" data-phase="drafting">
  <span class="phase-icon"><span class="spinner"></span></span>
  <span class="phase-label">Drafting</span>
</div>
```

**`linting` (first pass - no iterations yet):**
```html
[queued done] [drafting done (Ns)] [linting active]
```

**`iterating`:**
```html
[queued done] [drafting done] [linting done]
<div class="phase-row phase-row--active" data-phase="iterating">
  <span class="phase-icon"><span class="spinner"></span></span>
  <span class="phase-label">Iteration 1 - refining blocks</span>
</div>
```

**`qa`:**
```html
[all prior phases done] [qa active]
```

**`done` (qa.passed = true):**
```html
[all phases done with durations] 
<!-- output section becomes visible, no toast -->
```

**`done` (qa.passed = false):**
```html
[all phases done]
<!-- toast appears: yellow "QA found minor issues - output still generated" -->
<!-- output section becomes visible with amber QA badge -->
```

**`failed`:**
```html
[phases up to failure shown as done]
[last phase shown as failed with red icon]
<!-- error card shown below progress -->
```

---

## 5. Error State Spec

### Inline field error (empty input)
- Trigger: Generate clicked, textarea is blank or whitespace only
- Show `#input-error` div
- Add `.error` class to textarea (red border + shadow)
- Clear error when user starts typing
- No API call made

### Toast notification
- Position: fixed, bottom 24px, right 24px
- Width: 320px max
- Background: var(--bg-el), border 1px solid var(--bd), border-radius var(--r-lg), box-shadow var(--sh-md)
- Auto-dismiss after 5s
- Animation: slide up from bottom-right, fade out
- HTML:
```html
<div class="toast" role="alert" aria-live="polite" data-type="warn">
  <span class="toast-icon"><!-- SVG warning --></span>
  <span class="toast-message">QA found minor issues - output still generated</span>
  <button class="toast-close" aria-label="Dismiss" onclick="this.parentElement.remove()">x</button>
</div>
```

### Error card (failed state / timeout / quota)
Replaces progress area when terminal failure occurs:
```html
<div class="error-card" role="alert">
  <p class="error-title"><!-- error title --></p>
  <p class="error-detail"><!-- detail message --></p>
  <button class="btn btn-ghost btn-sm" onclick="retryGeneration()">Retry</button>
  <!-- OR no retry button for quota/cap errors -->
</div>
```

### Full error state table

| Error condition | `job.error` value | Display | Retry button |
|---|---|---|---|
| OpenAI timeout | `openai_timeout` | "Generation timed out. The model took too long to respond." | Yes |
| OpenAI quota | `openai_quota` | "OpenAI quota hit. Ping Mihajlo to top up." | No |
| Max iterations | `max_tool_calls` | "Linting couldn't converge in 10 passes. Retry or simplify the input." | Yes |
| Daily cap | HTTP 429 from POST /api/spintax | Yellow banner: "Daily cap reached ($20). Resets in {N}h {M}m." - injected above generate btn | No |
| Session expired (401 mid-job) | HTTP 401 from poll | Toast: "Session expired. Redirecting to login..." + `setTimeout(() => location='/login', 3000)` | No |
| Auth missing (page load) | HTTP 302 from GET / | Server-side redirect to /login - no JS needed | n/a |
| Malformed response | `malformed_response` | "Generation returned unexpected output. Retry?" | Yes |
| Job not found (poll 404) | HTTP 404 from poll | "Generation result lost. Retry?" | Yes |
| Lint PASS + QA fail | `done` with `qa.passed=false` | Yellow QA badge + toast "QA found minor issues" + output still rendered | n/a |

### Daily cap banner
```html
<div class="cap-banner" role="alert" style="display:none">
  <span>Daily generation cap reached ($20). Resets in <strong id="cap-reset-time">--</strong>.</span>
</div>
```
- CSS: `.cap-banner { padding: 12px 16px; background: var(--amber-bg); border: 1px solid rgba(184,122,0,0.2); border-radius: var(--r-lg); margin-bottom: 16px; font-size: 13px; color: var(--tx1); }`
- Shown when POST /api/spintax returns 429
- `cap-reset-time` populated from `ErrorEnvelope.details.resets_at`

---

## 6. Two Output Modes

The `spintax_body` string from the API is stored in a JS variable `window._rawSpintax`.
Mode toggle switches how it is displayed - it never re-requests the API.

### Raw spintax mode (default, `data-mode="raw"`)

**Block highlighting:**
Each `{{RANDOM | v1 | v2 | ... }}` block (Instantly format) and `{v1|v2|...}` block (EmailBison format) gets wrapped in a styled `<span>`.

The highlighting is applied by a JS function `highlightSpintax(text, platform)`:

```javascript
function highlightSpintax(text, platform) {
  // Instantly: {{RANDOM | v1 | v2 | ... }}
  // EmailBison: {v1|v2|...}
  const pattern = platform === 'instantly'
    ? /\{\{RANDOM\s*\|([^}]*)\}\}/g
    : /\{([^{}]+)\}/g;

  return text.replace(pattern, (match) => {
    const parts = platform === 'instantly'
      ? match.slice(9, -2).split('|')   // strip {{RANDOM| and }}
      : match.slice(1, -1).split('|');  // strip { and }
    const count = parts.length;
    const baseLen = parts[0].trim().length;
    return `<span class="spintax-block" data-count="${count}" data-baselen="${baseLen}">${escapeHtml(match)}<span class="spintax-chip">${count} var</span></span>`;
  });
}
```

**Block styling:**
```css
.spintax-block {
  position: relative;
  background: var(--blue-bg);
  border-radius: 4px;
  border: 1px solid var(--blue-bd);
  padding: 1px 2px;
  display: inline;
}

.spintax-chip {
  display: inline-block;
  background: var(--blue);
  color: #fff;
  font-size: 9px;
  font-weight: 700;
  padding: 1px 5px;
  border-radius: 3px;
  margin-left: 3px;
  vertical-align: middle;
  font-family: 'Space Mono', monospace;
  letter-spacing: 0.02em;
}
```

The `<pre id="raw-output">` uses `.innerHTML` to insert highlighted HTML.
Note: raw spintax from the API must be HTML-escaped before being set as innerHTML, with only spintax-block spans injected unsanitized (they are generated by our code, not user content).

### Preview variant mode (`data-mode="preview"`)

Uses the `randomize()` function verbatim from `spintax_compare_html.py` (JS block, lines 670-693), adapted to our DOM target:

```javascript
// COPIED VERBATIM from spintax_compare_html.py JS section, adapted for single target
function randomize(text, platform) {
  // Parse blocks from raw spintax text, pick one variant each, return assembled string
  const pattern = platform === 'instantly'
    ? /\{\{RANDOM\s*\|([^}]*)\}\}/g
    : /\{([^{}]+)\}/g;
  return text.replace(pattern, (match) => {
    const parts = platform === 'instantly'
      ? match.slice(9, -2).split('|').map(s => s.trim())
      : match.slice(1, -1).split('|').map(s => s.trim());
    if (!parts.length) return match;
    return parts[Math.floor(Math.random() * parts.length)];
  });
}

function randomizePreview() {
  const text = window._rawSpintax;
  const platform = window._platform;
  if (!text) return;
  document.getElementById('preview-output').textContent = randomize(text, platform);
}
```

**Note on `randomize()` vs the compare tool's version:**
The compare tool version operates on a pre-parsed JSON array of variations. Our version operates directly on the raw spintax string. This is the correct adaptation - the builder should use this version, not the exact JS from the compare tool (which assumed a different data shape).

**Mode switching:**
```javascript
function setMode(mode) {
  window._mode = mode;
  const rawContainer = document.getElementById('raw-container');
  const previewContainer = document.getElementById('preview-container');
  const randomizeBtn = document.getElementById('randomize-btn');
  const modeBtns = document.querySelectorAll('.mode-toggle .seg-btn');

  modeBtns.forEach(b => {
    const active = b.dataset.mode === mode;
    b.classList.toggle('seg-btn--active', active);
    b.setAttribute('aria-pressed', active ? 'true' : 'false');
  });

  if (mode === 'raw') {
    rawContainer.style.display = '';
    previewContainer.style.display = 'none';
    randomizeBtn.style.display = 'none';
  } else {
    rawContainer.style.display = 'none';
    previewContainer.style.display = '';
    randomizeBtn.style.display = '';
    randomizePreview();  // auto-roll on switch
  }
}
```

---

## 7. Copy + Download

**Copy button** always copies raw spintax regardless of current mode:
```javascript
async function copyRaw() {
  const text = window._rawSpintax;
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    const btn = document.getElementById('copy-btn');
    btn.textContent = 'Copied!';
    btn.classList.add('btn-ghost--success');
    setTimeout(() => {
      btn.textContent = 'Copy spintax';
      btn.classList.remove('btn-ghost--success');
    }, 2000);
  } catch {
    // Fallback for older browsers / non-HTTPS
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
  }
}
```

**Ghost button success state:**
```css
.btn-ghost--success {
  background: var(--green-bg) !important;
  color: var(--green) !important;
  border-color: rgba(26,138,62,0.3) !important;
  opacity: 1 !important;
}
```

**Download button** saves raw spintax as `.txt`:
```javascript
function downloadRaw() {
  const text = window._rawSpintax;
  if (!text) return;
  const ts = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
  const filename = `spintax_${ts}.txt`;
  const blob = new Blob([text], { type: 'text/plain' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}
```

---

## 8. Login Page Behavior

**Form submission:**
```javascript
async function submitLogin(event) {
  event.preventDefault();
  const btn = document.getElementById('login-btn');
  const passwordInput = document.getElementById('password');
  const errorDiv = document.getElementById('login-error');

  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-white"></span>Signing in...';
  errorDiv.style.display = 'none';
  passwordInput.classList.remove('error');

  try {
    const resp = await fetch('/admin/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: passwordInput.value })
    });

    if (resp.ok) {
      window.location.href = '/';
    } else {
      errorDiv.style.display = 'flex';
      passwordInput.classList.add('error');
      passwordInput.value = '';
      passwordInput.focus();
    }
  } catch {
    errorDiv.textContent = 'Network error. Check your connection.';
    errorDiv.style.display = 'flex';
  } finally {
    btn.disabled = false;
    btn.textContent = 'Sign in';
  }
}
```

**Enter key:** The `<form onsubmit="submitLogin(event)">` handles Enter naturally (form submit event fires on Enter in a single-input form). No additional keydown handler needed.

**Already logged in:** Server handles this - `GET /login` redirects to `/` if valid cookie. No JS needed.

---

## 9. Routes Spec (for builder)

### `GET /`

```python
@router.get("/")
async def index(request: Request):
    """Serve main UI. Auth-gated."""
    from app.auth import verify_cookie
    session_cookie = request.cookies.get("session")
    if not session_cookie or not verify_cookie(session_cookie):
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("index.html", {"request": request})
```

Route lives in `app/routes/pages.py` (new file, Phase 3 only).
Mount on `app.main` as `pages_router` (no prefix, public visibility).

### `GET /login`

```python
@router.get("/login")
async def login_page(request: Request):
    """Serve login form. If already authed, redirect to /."""
    from app.auth import verify_cookie
    session_cookie = request.cookies.get("session")
    if session_cookie and verify_cookie(session_cookie):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request})
```

Same file (`app/routes/pages.py`). Public (no `Depends(require_auth)`).

### Jinja2 setup

Add to `app/main.py`:
```python
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")
```

Templates use `{{ url_for('static', path='main.css') }}` for assets (FastAPI static URL pattern).

**Critical:** spintax output NEVER goes through Jinja. It is set via JS `.textContent` or `.innerHTML` from the JSON poll response. This avoids the `{{firstName}}` Jinja conflict documented in the planning session.

---

## 10. JavaScript Architecture

Single file: `static/main.js`. No modules, no import/export, no build step.

### Global state (window-scoped)
```javascript
window._jobId = null;        // current job UUID
window._pollInterval = null; // setInterval handle
window._rawSpintax = null;   // raw spintax string from API
window._platform = 'instantly';  // current platform selection
window._mode = 'raw';        // current output mode
window._startTime = null;    // Date.now() when job kicked off
```

### State machine (JS side)

```javascript
const UI_STATES = ['idle', 'queued', 'drafting', 'linting', 'iterating', 'qa', 'done', 'failed'];

function setState(state, data = {}) {
  // Update data-state on both elements
  document.getElementById('progress').dataset.state = state;
  document.body.dataset.state = state;

  // Dispatch to per-state handler
  switch(state) {
    case 'idle':     handleIdle(); break;
    case 'queued':   handleQueued(); break;
    case 'drafting': handleDrafting(); break;
    case 'linting':  handleLinting(); break;
    case 'iterating': handleIterating(data); break;
    case 'qa':       handleQA(); break;
    case 'done':     handleDone(data); break;
    case 'failed':   handleFailed(data); break;
  }
}
```

### Polling loop

```javascript
async function startGeneration() {
  const body = document.getElementById('email-body').value.trim();
  if (!body) {
    showInputError();
    return;
  }

  hideInputError();
  setState('queued');
  setGenerateButtonLoading(true);

  try {
    const resp = await fetch('/api/spintax', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        text: body,
        platform: window._platform
      })
    });

    if (resp.status === 429) {
      const data = await resp.json();
      showCapBanner(data.details);
      setState('idle');
      setGenerateButtonLoading(false);
      return;
    }

    if (resp.status === 401) {
      showToast('Session expired. Redirecting to login...', 'warn');
      setTimeout(() => { window.location.href = '/login'; }, 3000);
      return;
    }

    if (!resp.ok) {
      setState('failed', { error: 'submit_failed', message: 'Failed to start generation.' });
      setGenerateButtonLoading(false);
      return;
    }

    const { job_id } = await resp.json();
    window._jobId = job_id;
    window._startTime = Date.now();
    window._pollInterval = setInterval(pollStatus, 2000);

  } catch {
    setState('failed', { error: 'network_error', message: 'Network error. Check your connection.' });
    setGenerateButtonLoading(false);
  }
}

async function pollStatus() {
  if (!window._jobId) return;

  try {
    const resp = await fetch(`/api/status/${window._jobId}`);

    if (resp.status === 401) {
      clearInterval(window._pollInterval);
      showToast('Session expired. Redirecting to login...', 'warn');
      setTimeout(() => { window.location.href = '/login'; }, 3000);
      return;
    }

    if (resp.status === 404) {
      clearInterval(window._pollInterval);
      setState('failed', { error: 'job_not_found', message: 'Generation result lost. Retry?' });
      setGenerateButtonLoading(false);
      return;
    }

    const data = await resp.json();
    updateElapsedTime();

    const terminalStates = ['done', 'failed'];
    if (terminalStates.includes(data.status)) {
      clearInterval(window._pollInterval);
      window._pollInterval = null;
      setGenerateButtonLoading(false);
    }

    setState(data.status, data);

  } catch {
    // Network blip during poll - do not stop polling, just skip this tick
    console.warn('Poll failed, retrying...');
  }
}
```

### ETA display

```javascript
function updateElapsedTime() {
  const elapsed = Math.floor((Date.now() - window._startTime) / 1000);
  const metaEl = document.getElementById('progress-meta');
  if (!metaEl) return;
  if (elapsed < 30) {
    metaEl.textContent = '';
    return;
  }
  metaEl.textContent = `${elapsed}s elapsed - o3 usually takes 60-170s`;
}
```

---

## 11. Accessibility Decisions

### Color contrast
All text/background combinations hit WCAG AA (4.5:1 for normal text, 3:1 for large text):
- `--tx1` (#1a1a1a) on `--bg-el` (#ffffff): ~18:1 (AAA)
- `--tx2` (#6b6b6b) on `--bg-el` (#ffffff): ~5.7:1 (AA)
- `--tx3` (#8a8a8e) on `--bg-el` (#ffffff): ~3.7:1 (AA large text only - use sparingly for labels)
- White (#fff) on `--blue-d` (#0a61d1) button: ~6:1 (AA)

### Focus states
All interactive elements get explicit focus rings. Do not rely on browser defaults (they differ):
```css
:focus-visible {
  outline: 2px solid var(--blue);
  outline-offset: 2px;
  border-radius: var(--r-sm);
}
```
Remove focus ring on mouse click only (`:focus:not(:focus-visible)`).

### ARIA labels for icon-only actions
Even though our action buttons have text labels ("Copy spintax", "Download .txt", "Re-roll preview"),
ensure aria-label is present as a belt-and-suspenders measure.

### Screen reader announcements
- `#progress` section: `aria-live="polite"` - announces state changes
- `#input-error`: `role="alert" aria-live="assertive"` - announces validation errors
- `#output-word-count`: `aria-live="polite"` - announces word count updates
- Toast: `role="alert" aria-live="polite"`

### Keyboard navigation order
Tab sequence:
1. Logo (skip link optional)
2. Textarea
3. Platform segmented control buttons (arrow keys navigate within group)
4. Generate button
5. (When output visible) Mode toggle buttons
6. Copy spintax button
7. Download .txt button
8. Re-roll preview button (when in preview mode)

Segmented controls: `role="group"` on container, arrow key navigation within (left/right switches selection).

### Reduced motion
```css
@media (prefers-reduced-motion: reduce) {
  .spinner, .spinner-white { animation: none; border-style: dashed; }
  .toast { transition: none; }
  * { transition-duration: 0.01ms !important; animation-duration: 0.01ms !important; }
}
```

---

## 12. Responsive Breakpoints

**Desktop-first, mobile-functional:**

```css
/* Base (desktop 1440px - primary use case) */
.container { max-width: 920px; margin: 0 auto; padding: 32px 24px 80px; }
.topbar-inner { max-width: 920px; }
textarea { min-height: 240px; }
.login-card { width: 360px; }

/* Tablet (768px) */
@media (max-width: 768px) {
  .container { padding: 24px 20px 60px; }
  textarea { min-height: 200px; }
}

/* Mobile (375px - minimum supported) */
@media (max-width: 600px) {
  .container { padding: 16px 16px 48px; }
  .topbar { padding: 12px 16px; }
  h1 { font-size: 20px; }
  textarea { min-height: 160px; font-size: 13px; }
  .input-meta { flex-direction: column; align-items: flex-start; gap: 10px; }
  .word-count { margin-left: 0; }
  .output-header { flex-direction: column; align-items: flex-start; gap: 10px; }
  .output-actions { flex-wrap: wrap; }
  .output-actions .word-count { margin-left: 0; width: 100%; }
  .login-card { padding: 32px 24px; }
}
```

---

## 13. Rationale Section

### Why two output modes (raw + preview) and not one?

The two modes serve fundamentally different jobs. Raw spintax is the deliverable - users copy it into Instantly. Preview variant is a quality check - users want to see "does this read as a real email?" before shipping. Collapsing these into one view would either hide the spintax structure (making QA harder) or force users to stare at `{{RANDOM | ...}}` blocks to mentally simulate an email. Separate modes with a quick toggle costs one segmented control and 20 lines of JS. Worth it every time.

### Why segmented control vs radio buttons for platform?

Radio buttons imply a settings form - they're appropriate for configuration screens. This is a quick binary toggle that changes how the tool behaves for a single generation. Segmented control is the correct pattern for this: two mutually exclusive options, immediate visual feedback, feels like a toggle. The spam-checker has no such choice, so there is no precedent to follow here - we pick the right pattern independently.

### Why polling vs SSE?

SSE (server-sent events) would give smoother UX: real-time state updates as they happen. But SSE requires a persistent connection that Render's load balancer may cut at 30-100s (Render free tier). Our o3 generations run 60-170s. We already have a working polling job store from Phase 2. Polling at 2s adds at most 1.9s of display lag per state transition - imperceptible on a 100s generation. SSE is in the Phase 2 deferrals list and stays there. Polling is the pragmatic right choice for this deploy target.

### Why server-side template render vs SPA?

SPAs require a build step, npm dependency management, and separate deploy concerns. This tool is used by a small internal team. Server-side Jinja2 templates with HTMX for partial updates (if ever needed) is the right pattern. The output rendering is done in JS (not Jinja) specifically to avoid the `{{firstName}}` Jinja conflict documented in the planning session. We get the best of both: server renders the shell, JS handles the dynamic output display.

### Why full-width Generate button?

The generate button is the primary action. Full-width buttons on single-action forms are a well-established mobile and web pattern that increases the tap target size and signals primacy. On desktop at 920px max-width, a full-width button still looks intentional and clean (not stretched). Compare to spam-checker where the button is inline - appropriate there because "Clear" is a co-equal action next to it. Here, there is no co-equal action for generate.

### Why "Team" admin badge instead of a logout button?

This is a team tool with a shared password. There is no per-user identity, so there is nothing meaningful to "log out" of proactively. Users will naturally be logged out when the 7-day cookie expires. Adding a logout button would prompt users to wonder "should I log out after each use?" - not a behavior we want to encourage (it creates friction without security benefit for a shared-password tool). The badge serves as a visual confirmation that you ARE logged in, without adding interaction complexity.

---

## Verification Checklist (for p3_creative before handoff)

- [x] Token block copied verbatim from spam-checker style.css
- [x] All 9 job states have `data-state` attribute spec
- [x] All error conditions in Phase 2 spec have corresponding UI treatment
- [x] `randomize()` function adapted (not copied verbatim - data shape differs)
- [x] Jinja conflict mitigated: output uses `.textContent` / `.innerHTML` from JSON, never Jinja
- [x] Lint PASS + QA fail = yellow badge + toast + output rendered (T8 contract honored)
- [x] No emdashes in this document
- [x] Login redirect: server-side 302, no JS race condition
- [x] Copy always copies raw spintax regardless of mode
- [x] Accessibility: aria-live, role=alert, focus-visible, reduced-motion

---

*End of design contract. Builder picks this up at Section 9 (Routes Spec) to understand the server-side changes, then Sections 2-8 for all HTML/CSS/JS details.*
