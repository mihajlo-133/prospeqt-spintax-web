---
title: "Phase 3 Playwright QA Report"
type: output
tags: [qa, playwright, phase-3, spintax-web]
status: active
created: 2026-04-27
---

# Phase 3 Playwright QA Report

**Date:** 2026-04-27  
**Agent:** p3_playwright  
**App URL:** http://localhost:8086  
**Tool:** @playwright/cli v1.59.0-alpha  
**Viewports tested:** Desktop (1440x900), Tablet (768x1024), Mobile (375x812)  
**Screenshots:** `qa/screenshots/p3/` (26 screenshots, all visually reviewed)

---

## Summary

| Journey | Desktop | Tablet | Mobile | Result |
|---------|---------|--------|--------|--------|
| J1: Login page renders | PASS | PASS | PASS | PASS |
| J2: Wrong password error | PASS | PASS | PASS | PASS |
| J3: Main page loads after auth | PASS | PASS | PASS | PASS |
| J4: Validation error (empty submit) | PASS | PASS | PASS | PASS |
| J5: Generate happy path (mocked) | PASS | PASS | PASS | PASS |
| J6: Output mode toggle + re-roll | FAIL | FAIL | FAIL | FAIL |
| J7: Copy button feedback | PASS | - | - | PASS |
| J7: Download .txt | PASS | - | - | PASS |
| J8: Error state display + Retry | PASS | PASS | PASS | PASS |

**Overall: FAIL - 2 bugs, 1 of which is WARNING severity**

---

## Bug List

### BUG-01 - WARNING: Preview mode does not resolve spintax variants

**Severity:** WARNING  
**Journey:** J6 - Output mode toggle  
**Viewports affected:** All (Desktop, Tablet, Mobile)  
**Screenshots:** `07_preview_desktop.png`, `07_preview_mobile.png`, `08_rerolled_desktop.png`

**Description:**  
When the user clicks "Preview variant", the output area continues to display the raw spintax string with curly-brace syntax (e.g., `{Hi|Hey|Hello} {{firstName}}, I {saw|noticed|came across}...`) instead of resolving one random variant to plain text (e.g., "Hey John, I noticed your company..."). The "Re-roll preview" button also has no effect - clicking it does not change the displayed text in either the accessibility tree or visually.

**Expected behavior:**  
"Preview variant" mode should randomly pick one option from each `{a|b|c}` group and render the resolved plain-text email, giving the user a realistic preview of what recipients will see. "Re-roll" should resolve a new random combination.

**Actual behavior:**  
Both Raw mode and Preview mode display identical content - the raw spintax body. Re-roll does nothing. The mode toggle button states (pressed/active) do update correctly, but the output content does not change.

**DOM evidence:**  
- Raw mode snapshot: `ref=e159` text = `{Hi|Hey|Hello} {{firstName}}, I {saw|noticed|came across}...`  
- Preview mode snapshot: `ref=e165` text = `{Hi|Hey|Hello} {{firstName}}, I {saw|noticed|came across}...` (identical)  
- After re-roll: `ref=e165` text unchanged

**Fix required:**  
`static/main.js` needs a `resolveSpintax(body)` function that picks one random option from each `{a|b|c}` group (treating `{{variable}}` as a pass-through). Called on mode switch to Preview and on Re-roll click. The resolved text should replace the output element's `textContent` in Preview mode.

---

### BUG-02 - WARNING: Generate button not disabled when textarea is empty

**Severity:** WARNING  
**Journey:** J4 - Validation error  
**Viewports affected:** All  
**Screenshots:** `04_validation_error_desktop.png`

**Description:**  
The Generate button is clickable when the textarea is empty. Clicking it triggers inline validation (an error message appears), but the button itself is not disabled in empty state. The spec says "Generate button is in disabled state when textarea empty."

**Expected behavior:**  
`<button disabled>` or `aria-disabled="true"` on the Generate button when textarea has 0 characters. User should not be able to click it until they enter text.

**Actual behavior:**  
Button is enabled at all times. Inline validation fires on click as a fallback.

**DOM evidence:**  
Snapshot at empty textarea shows: `button "Generate spintax" [ref=e25] [cursor=pointer]` - no `[disabled]` attribute.

**Note:** The inline validation itself works correctly and is good UX as a second line of defense. But the disabled state is a spec requirement and prevents unnecessary click handling.

---

### BUG-03 - NIT: favicon.ico returns 404

**Severity:** NIT  
**Journey:** All (fires on every page load)

**Description:**  
Every page load logs a 404 in the browser console for `/favicon.ico`. No favicon is served.

**Fix:** Add a 32x32 favicon to `static/` and serve it, or add a `<link rel="shortcut icon" href="data:,">` to the base template to suppress the 404.

---

### BUG-04 - NIT: Login form accessibility warning

**Severity:** NIT  
**Journey:** J1, J2 - Login  

**Description:**  
Browser logs a VERBOSE accessibility warning: "Password forms should have (optionally hidden) username fields for accessibility." The login form may be missing a username/email `<input>` before the password field, or the association is not properly marked up.

**Fix:** Ensure the login form has `autocomplete="username"` on the email/username field and `autocomplete="current-password"` on the password field.

---

## Per-Journey Results

### Journey 1: Login page renders (PASS)
**Screenshots:** `01_login_desktop.png`, `01_login_tablet.png`, `01_login_mobile.png`  
All viewports: Login form renders cleanly. Password field present. Submit button present. No horizontal scroll at 375px. Responsive layout correct.

### Journey 2: Wrong password shows error (PASS)
**Screenshots:** `02_login_error_desktop.png`, `02_login_error_tablet.png`, `02_login_error_mobile.png`  
Wrong password (`wrongpass`) → 401 response → inline error message displayed. Error message is visible at all viewports. The 401 logs as a console error (expected browser behavior for failed fetch, not a bug).

### Journey 3: Main page loads after auth (PASS)
**Screenshots:** `03_main_loaded_desktop.png`, `03_main_loaded_tablet.png`, `03_main_loaded_mobile.png`  
Correct password → authenticated → redirected to `/`. Page shows:
- "Spintax Generator" heading
- Plain email body textarea
- Platform toggle (Instantly / EmailBison)
- Word count ("0 words")
- "Generate spintax" button  
No horizontal scroll at 375px. Layout correct at all viewports.

### Journey 4: Validation error on empty submit (PASS with caveat)
**Screenshots:** `04_validation_error_desktop.png`, `04_validation_error_tablet.png`, `04_validation_error_mobile.png`  
Empty textarea → click Generate → inline validation error appears. Error is visible at all viewports.  
**Caveat:** See BUG-02 - button should be disabled before click, not just validate-on-click.

### Journey 5: Generate happy path (PASS)
**Screenshots:** `05_progress_desktop.png`, `05_progress_tablet.png`, `05_progress_mobile.png`, `06_output_done_desktop.png`, `06_output_done_tablet.png`, `06_output_done_mobile.png`  
(Tested with mocked API - no real OpenAI calls made)

**Generate flow:**
- POST `/api/spintax` → returns `job_id`
- UI immediately shows progress panel with 4 steps (Queued, Drafting, Linting, Running QA)
- Button changes to "Generating..." and disables
- Textarea disables
- Elapsed timer shows ("44s elapsed - o3 usually takes 60-170s")

**Done state:**
- All 4 steps show green checkmarks
- Output section appears with "Lint PASS" and "QA PASS" badges
- Cost/call/time metadata shown ("$0.04 - 3 calls - 47s")
- Raw spintax rendered in monospace output box
- Copy, Download, Re-roll buttons present
- Raw/Preview toggle present

All correct at all 3 viewports.

### Journey 6: Output mode toggle + re-roll (FAIL)
**Screenshots:** `07_preview_desktop.png`, `07_preview_tablet.png`, `07_preview_mobile.png`, `08_rerolled_desktop.png`  
Toggle between Raw and Preview modes: button states update correctly (pressed/active attributes switch).  
**FAIL:** Preview mode does not resolve spintax to a plain variant. Output text is identical in both modes. Re-roll has no effect. See BUG-01.

### Journey 7: Copy + Download (PASS)
**Screenshots:** `09_copied_desktop.png`  

**Copy:** Click "Copy spintax" → accessible name changes to "Copied!" → button shows pressed/active state. Clipboard contents not directly verifiable via CLI, but feedback state confirms the action fired.

**Download:** Click "Download .txt" → file downloaded as `spintax_2026-04-27T08-31-36.txt`. File content verified:
```
{Hi|Hey|Hello} {{firstName}},

I {saw|noticed|came across} your company {{companyName}} is {hiring|growing|expanding}. {We help teams|Our platform helps} like yours save time. {Would love to|Happy to} connect and share how.

{Best|Regards},
Alex
```
Content is correct raw spintax with proper formatting and line breaks. PASS.

### Journey 8: Error state display + Retry (PASS)
**Screenshots:** `11_error_timeout_desktop_full.png`, `11_error_timeout_tablet.png`, `11_error_timeout_mobile.png`  
(Tested with mocked `/api/status` returning `status:"failed"`)

**Error display:**
- Queued step: green checkmark
- Drafting step: red X (failure point) highlighted in red text
- Linting, Running QA: grey dots (not reached)
- Red-tinted alert box: "Generation failed" heading + "Something went wrong. Retry?" + Retry button
- ARIA `role="alert"` on error container (accessibility correct)

**Retry button:** Click → textarea re-enables, button resets to "Generating..." (disabled), new job started. State machine reset confirmed.

All correct at all 3 viewports. No horizontal scroll at 375px.

---

## Console Error Log

| Error | Type | Journey | Severity |
|-------|------|---------|----------|
| `favicon.ico 404` | Network error | All pages | NIT |
| `401 Unauthorized @ /admin/login` | Network error | J2 (wrong password) | Expected - not a bug |
| Password form accessibility warning | DOM verbose | J1, J2 | NIT |

**No JavaScript runtime errors observed.** No uncaught exceptions. No CORS errors.

---

## Accessibility Notes

- Error container correctly uses ARIA `role="alert"` for screen readers
- Platform toggle uses `<group>` with "Target platform" label (ARIA group)
- Button accessible names are descriptive ("Copy raw spintax to clipboard", not just "Copy")
- Generate button has `[disabled]` attribute during generation (keyboard users cannot tab to it)
- Login password form has accessibility warning (see BUG-04)

---

## Responsive Layout Notes

| Viewport | Issues Found |
|---------|-------------|
| Desktop 1440x900 | None |
| Tablet 768x1024 | None |
| Mobile 375x812 | None - no horizontal scroll at any point in any journey |

---

## Fix Priority

1. **BUG-01 (WARNING)** - Preview variant resolution - user-facing, breaks a promoted feature
2. **BUG-02 (WARNING)** - Generate button not disabled on empty - spec requirement
3. **BUG-03 (NIT)** - favicon 404 - minor, quick fix
4. **BUG-04 (NIT)** - Login form accessibility - minor markup fix

---

## STATUS: P3 PLAYWRIGHT QA COMPLETE - FIX LIST BELOW

**2 WARNING bugs must be fixed before Phase 3 can be marked shipped:**

1. **BUG-01**: `static/main.js` - implement `resolveSpintax()` function. On toggle to Preview mode and on Re-roll click, pick one random option from each `{a|b|c}` group (skip `{{doublebraces}}`) and replace output textContent.

2. **BUG-02**: `static/main.js` or `templates/index.html` - add input event listener to textarea; set Generate button `disabled` attribute when `textarea.value.trim() === ''`, remove when not empty.
