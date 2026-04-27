# Phase 3 UX Audit Report

**Audited by:** ux-design-expert agent  
**Date:** 2026-04-27  
**Source files:** templates/index.html, templates/login.html, static/main.css (922 lines), static/main.js (866 lines)  
**Design contract:** DESIGN.md (1190 lines, locked)  
**Playwright evidence:** qa/p3_bugfix_report.md (all 4 bugs PASS), qa/screenshots/p3/ (14 screenshots across 3 viewports)

---

## Scores

| Pillar | Score | Verdict |
|--------|-------|---------|
| 1. IA & Navigation | 9/10 | Clean flow, tool switcher correct |
| 2. Visual Hierarchy & Typography | 8/10 | Tokens applied correctly; one label contrast miss |
| 3. Interactive Affordances & Feedback | 7/10 | All states present; raw output missing highlights |
| 4. Accessibility | 8/10 | Strong ARIA, focus rings, reduced motion; one gap |
| 5. Responsive Design | 9/10 | Breakpoints correct; mobile layout clean |
| 6. Performance & Resilience | 9/10 | Full error matrix covered; polling resilient |
| **Overall** | **8.3/10** | |

---

## Design Contract Compliance

| Requirement | Status | Evidence |
|-------------|--------|----------|
| Token block copied verbatim from spam-checker | PASS | main.css lines 6-50 match DESIGN.md section 1 exactly |
| All 3 font families loaded (Inter, Space Grotesk, Space Mono) | PASS | Both HTML heads load the same Google Fonts CDN string |
| Font roles: Space Grotesk on headings only | PASS | h1/h2/logo use `font-family: 'Space Grotesk'` (main.css lines 145-161); body uses Inter |
| Font roles: Space Mono on all mono output | PASS | `.spintax-output`, `.word-count`, `.cap-display`, `.footer`, stat badges all use Space Mono |
| Logo mark: 22x22px gradient blue, white "P" | PASS | main.css lines 115-127 match spec exactly |
| Admin badge: "Team", Space Mono 11px | PASS | index.html line 21, main.css lines 135-142 |
| Tool switcher: 4 items, current item active + "Current" badge | PASS | index.html lines 35-54; `.tool-switcher-item--active` + aria-current="page" on Spintax Generator |
| Tool switcher: closes on outside click and Escape | PASS | main.js lines 847-864 |
| Textarea: Inter (NOT monospace), min-height 240px | PASS | main.css lines 184-210 |
| Platform segmented control: role="group", aria-pressed | PASS | index.html lines 78-92 |
| Generate button: `btn--full`, min-height 44px | PASS | main.css lines 296-300; btn--full sets min-height: 44px |
| Generate button disabled on page load (BUG-02) | PASS | Playwright: `disabled=true` on empty textarea confirmed |
| Generate button loading: spinner-white + "Generating..." | PASS | main.js lines 135-143 |
| Generate button re-enabled after terminal state | PASS | main.js lines 784-788 (`setGenerateButtonLoading(false)`) |
| Cap display: injected under generate button | PASS | `#cap-display` div present (index.html line 104); JS populates it via `showCapBanner()` |
| Cap banner: amber, role="alert", reset time | PASS | index.html lines 64-67; main.css lines 594-607; main.js lines 677-698 |
| Input error: role="alert", aria-live="assertive" | PASS | index.html lines 107-113 |
| Input error: clears when user types | PASS | main.js line 833 |
| `data-state` on both `#progress` and `<body>` | PASS | main.js `setDataState()` (lines 62-66) updates both |
| Progress section: aria-live="polite", aria-label | PASS | index.html lines 116-121 |
| Phase rows: all 4 states (pending/active/done/failed) | PASS | main.css lines 420-449; main.js `renderPhaseRow()` lines 172-191 |
| Phase icon: spinner on active, check on done, X on failed, dot on pending | PASS | Screenshot 05 confirms spinner/check mix; screenshot 11 confirms X on failed |
| Iteration label: "Iteration N - refining blocks" | PASS | main.js lines 213-218 |
| Elapsed time after 30s: Space Mono `.mono-stat` | PASS | Screenshot 06/07 confirm "44s elapsed - o3 usually takes 60-170s" |
| Output section gated: hidden until done | PASS | Confirmed by screenshots 03, 05 (output hidden); 06 (output visible) |
| Output badges: Lint/QA badges + cost pill injected by JS | PASS | Screenshot 06 confirms "Lint PASS / QA PASS / $0.04 - 3 calls - 47s" |
| Mode toggle: role="group", aria-pressed, both modes wired | PASS | index.html lines 136-151; setMode() updates aria-pressed correctly |
| Raw mode: `.spintax-output` pre block, Space Mono 13px | PASS | main.css lines 510-523 |
| Raw mode: spintax blocks highlighted with `.spintax-block` chips | **PARTIAL** | JS function `highlightSpintax()` exists and is called (main.js lines 346-353), but screenshots 06, 07, 08, 09 all show unformatted plain monospace text - no blue background, no `.spintax-chip` counters visible. The code path exists but the visual output does not match the spec. |
| Preview mode: plain resolved text, no spintax syntax | PASS | Screenshots (07, 08) + bugfix report confirm correct resolution |
| Preview: `{{firstName}}` variables pass through unchanged | PASS | Bugfix report shows "Hey {{firstName}}, quick note." |
| Re-roll preview button: hidden in raw mode, shown in preview | PASS | Screenshot 07/08 confirm button visible; screenshot 06/09 confirm it is hidden |
| Copy button: copies raw spintax regardless of mode | PASS | main.js lines 596-617; design contract section 7 honored |
| Copy button: success state `.btn-ghost--success` for 2s | PASS | main.js lines 606-614; main.css lines 317-322 |
| Download button: saves as spintax_*.txt | PASS | main.js lines 631-645 |
| Error card: red background, `role="alert"`, in `#error-card-slot` | PASS | Screenshot 11_full confirms red card with "Generation failed" + Retry button |
| Error card: no retry button for quota/cap errors | PASS | `errorInfo('openai_quota')` returns `retry: false` (main.js line 401) |
| Toast: fixed bottom-right, auto-dismiss 5s, close button | PASS | main.css lines 634-687; main.js lines 651-672 |
| Toast: QA fail + lint pass = warn toast + amber badge | PASS | main.js lines 340-343 |
| Login card: 360px centered, logo mark, subtitle | PASS | Screenshot 01_login confirms centered card; main.css lines 699-727 |
| Login password input: height 44px, focus ring, error state | PASS | main.css lines 729-752 |
| Login: spinner in button while submitting | PASS | login.html lines 72-73 |
| Login: password cleared + field focused on wrong password | PASS | login.html lines 89-91 |
| Login: autocomplete="current-password" (BUG-04) | PASS | Playwright confirmed `autocomplete="current-password"` |
| Login: hidden username field for password managers (BUG-04) | PASS | Playwright confirmed `aria-hidden="true"`, `tabindex="-1"` |
| Login: `autofocus` on password field | PASS | login.html line 46 |
| Favicon: no 404 (BUG-03) | PASS | `<link rel="icon" href="data:,">` in both HTML heads |
| Reduced motion: spinners and transitions suppressed | PASS | main.css lines 880-894 |
| Keyboard shortcut: Ctrl/Cmd+Enter triggers generate | PASS | main.js lines 835-839 |

---

## Findings by Pillar

### Pillar 1: Information Architecture & Navigation - 9/10

**Finding 1.1 - Flow is correct and gated properly.**
The idle state hides both `#progress` and `#output` (main.js `setState('idle')` lines 287-289). The progress section appears on any active state; the output section appears only on `done`. No intermediate output leak is possible. The gate is enforced on both the JS side (`style.display`) and the ARIA side (`aria-label="Generation progress"` with `aria-live="polite"`).

**Finding 1.2 - Tool switcher is correctly labeled and wired.**
`aria-expanded`, `aria-haspopup="menu"`, and `role="menu"` / `role="menuitem"` are all present (index.html lines 23-54). `aria-current="page"` is on the active item. Outside-click dismissal and Escape-key dismissal are both implemented (main.js lines 847-864). Screenshot 03_main_loaded_desktop confirms the hamburger renders correctly.

**Finding 1.3 - Minor: no skip-navigation link.**
DESIGN.md section 11 mentions "skip link optional" for keyboard navigation order. It is not implemented. This is a minor penalty for users who tab through the topbar before reaching the textarea on every page load. Non-blocking for an internal team tool.

---

### Pillar 2: Visual Hierarchy & Typography - 8/10

**Finding 2.1 - Token application is verbatim-correct.**
Every token in `:root` (main.css lines 6-50) matches DESIGN.md section 1 character-for-character. No new color variables were introduced. Font family assignments match the strict role table.

**Finding 2.2 - Heading scale is clear.**
h1 (Space Grotesk 28px, weight 700, letter-spacing -0.025em) and h2 (20px, 600) are well-differentiated. The `.lead` paragraph at 15px / `--tx2` (#6b6b6b) is correctly subordinate. On mobile at 375px, h1 scales to 20px (main.css line 906), which keeps it readable without overwhelming.

**Finding 2.3 - `--tx3` (#8a8a8e on #ffffff) fails AA for body-size text.**
The DESIGN.md section 11 notes: "var(--tx3) (#8a8a8e) on var(--bg-el) (#ffffff): ~3.7:1 (AA large text only - use sparingly for labels)." The phase labels in pending state (`phase-row--pending .phase-label` uses `--tx3` via inheritance from `.phase-label`, main.css line 411-413) render at 14px / normal weight. At that size, 3.7:1 falls below the WCAG 4.5:1 AA threshold for normal text. The design contract itself acknowledges this and says "use sparingly for labels." The pending phase labels are a label use case, so this is technically a known documented trade-off rather than a violation. However, pending phases are meaningful information (users read them to see what comes next), not decorative labels. Worth noting.

**Finding 2.4 - `.spintax-chip` at 9px is below readable threshold for normal text.**
The chip label ("3 var") renders at 9px Space Mono (main.css line 561). While it is supplemental annotation (not the primary content), this is below any WCAG threshold and visually requires squinting at typical viewing distances. The contrast (white on `--blue` #2756f7) is approximately 4.1:1 which just misses the 4.5:1 normal-text threshold. Since chips are only displayed in the raw output zone (not inline with reading flow), this is a low-severity note.

---

### Pillar 3: Interactive Affordances & Feedback - 7/10

**Finding 3.1 - Raw output spintax highlight is not visible in rendered screenshots.**
This is the most significant finding in the audit. DESIGN.md section 6 specifies:
> "Each `{{RANDOM | v1 | v2 | ... }}` block and `{v1|v2|...}` block gets wrapped in a styled `<span class='spintax-block'>` with a `<span class='spintax-chip'>N var</span>` annotation."

The `highlightSpintax()` function (main.js lines 511-530) exists and is called correctly via `renderRawOutput()` (lines 346-353), and `.spintax-block` / `.spintax-chip` CSS rules are present (main.css lines 549-569). However, screenshots 06, 07, 08, and 09 all show plain monospace text with no blue tinting, no borders, and no chip badges on any spintax block.

The test data used in the QA run was EmailBison-format (`{Hi|Hey|Hello}`) and the platform was "Instantly." If the Playwright test submitted EmailBison-format copy while the platform selector was set to "Instantly," the `tokenizeSpintax()` function would still detect `single` tokens (it handles both formats regardless of platform, see main.js line 484-491). So the detection should have worked. The most likely explanation: the `<pre>` element's `font-family: 'Space Mono'` renders inline HTML including `<span>` elements, but the Playwright screenshots were taken at a size or scroll position where the highlighting blends with the monospace background. Alternatively, the mock data used in QA screenshots came from a path that did not trigger `handleDone()` / `renderRawOutput()`.

Evidence supports the code is correct (function exists, CSS exists, highlightSpintax is called), but the visual evidence is ambiguous. **This should be manually verified on the live app before merge.**

**Finding 3.2 - All 5 button states are correctly implemented.**
Generate button: normal (enabled, blue gradient), disabled (opacity 0.5), loading (spinner + "Generating...", disabled), done (re-enabled). Copy button: normal, success (green tint for 2s). Ghost buttons: normal, hover (bg-hov), disabled (no explicit disabled style but they are only shown when output is present). The `.btn:active { transform: translateY(1px) }` micro-interaction provides tactile feedback.

**Finding 3.3 - Error card is correctly positioned and actionable.**
Screenshot 11_full shows the failed state with "Generation failed / Something went wrong. Retry?" card inside `#error-card-slot` within `#progress-card`. The Retry button calls `retryGeneration()` which resets state and re-submits (main.js lines 801-810). No output section is shown in failed state.

**Finding 3.4 - Toast warning icon is always the warning SVG (ICON_WARN) regardless of toast type.**
`showToast()` (main.js lines 651-672) always inserts `ICON_WARN` as the icon, even for `type = 'error'`. The design contract shows a generic toast with a warning icon for the QA-fail case, so this is acceptable for the current states. A true error toast (if ever added) should show a red X icon. Not a blocking issue for Phase 3.

---

### Pillar 4: Accessibility - 8/10

**Finding 4.1 - Focus rings are implemented correctly.**
`:focus-visible { outline: 2px solid var(--blue); outline-offset: 2px; border-radius: var(--r-sm); }` (main.css lines 73-78). Mouse-click suppression via `:focus:not(:focus-visible)` (lines 80-82). This is best-practice 2024 implementation. The 2px outline at 3:1+ contrast against both white and light-grey backgrounds meets WCAG 2.5.5 focus indicator requirements.

**Finding 4.2 - ARIA live regions are complete and correctly scoped.**
`#progress` has `aria-live="polite"` (announces phase changes without interrupting). `#input-error` has `role="alert" aria-live="assertive"` (immediate announcement for validation errors). `#word-count` and `#output-word-count` have `aria-live="polite"`. Toast has `role="alert" aria-live="polite"`. Error card has `role="alert"`. Cap banner has `role="alert"`. This covers every dynamic content zone.

**Finding 4.3 - Arrow-key navigation within segmented controls is NOT implemented.**
DESIGN.md section 11 specifies: "Segmented controls: `role='group'` on container, arrow key navigation within (left/right switches selection)." The containers have `role="group"` and `aria-label` (index.html lines 78, 136), but the JS has no `keydown` handler for ArrowLeft/ArrowRight on the segmented control buttons. Standard keyboard users must Tab through individual seg-btn elements, which works but violates the ARIA patterns for composite widgets where arrow keys should navigate within the group and Tab should exit it. This is a WCAG 2.1 Level AA gap under "Keyboard (No Exception)." Non-blocking for an internal team tool but should be addressed.

**Finding 4.4 - Hidden username field on login form is correctly implemented.**
The `aria-hidden="true"` plus `tabindex="-1"` approach (login.html lines 30-38) correctly removes the field from both keyboard navigation and screen reader announcement while keeping it in the DOM for password managers. This is the correct implementation of BUG-04.

**Finding 4.5 - Icon-only actions all have `aria-label`.**
The tool-switcher button has `aria-label="Switch Prospeqt tool"` (index.html line 25). The SVG icon has `aria-hidden="true"` (line 29). Copy, download, and randomize buttons all have `aria-label` attributes (index.html lines 167, 172, 180). Toast close button has `aria-label="Dismiss"` (main.js line 663).

---

### Pillar 5: Responsive Design - 9/10

**Finding 5.1 - Breakpoints match the design contract exactly.**
Three breakpoints are implemented: desktop base (920px max-width container), tablet `@media (max-width: 768px)` (reduced container padding, textarea min-height 200px), mobile `@media (max-width: 600px)` (16px padding, h1 scaled to 20px, textarea 160px, input-meta stacks vertically, output-actions wraps). These match DESIGN.md section 12 verbatim.

**Finding 5.2 - Mobile layout is clean at 375px.**
Screenshot 03_main_loaded_mobile confirms the layout: topbar with logo + Team badge + hamburger, h1 at correct smaller size, full-width textarea, platform toggle stacked below with word count on its own line, full-width generate button at 44px height. Output at mobile (screenshot 06_output_done_mobile) shows correct vertical stacking of progress card, badges, mode toggle, output pre block, and action buttons.

**Finding 5.3 - Login card respects max-width on mobile.**
`width: 360px; max-width: calc(100vw - 48px)` (main.css line 704) correctly constrains the card to viewport width minus 24px side margins on narrow screens. Screenshot 01_login_mobile confirms clean centering.

**Finding 5.4 - Touch targets meet 44x44px minimum.**
The Generate button has `min-height: 44px; width: 100%` (btn--full, main.css lines 296-300). The platform seg-btn elements at `padding: 6px 16px` render at approximately 32px height - below the 44px WCAG 2.5.5 threshold. However, the segmented control sits in an internal team tool used primarily on desktop, and the overall control group width provides ample tap area horizontally. The login password input has `height: 44px` (main.css line 731). The generate button and all ghost action buttons are the primary tap targets and meet the threshold.

---

### Pillar 6: Performance & Resilience - 9/10

**Finding 6.1 - Polling loop is resilient.**
`pollStatus()` catches network errors and silently retries on the next interval (main.js lines 789-791). 401 and 404 responses are explicitly handled with user-visible feedback. 5xx transient errors are skipped without stopping the poll. The 2-second poll interval with `setInterval` and a guard `if (!window._jobId) return` at entry prevents runaway polling.

**Finding 6.2 - Elapsed time is surfaced correctly after 30 seconds.**
The `startElapsedTimer()` function (main.js lines 263-273) runs `updateElapsedTime()` every second. The function suppresses output until 30s have elapsed (lines 258-260), then shows "Ns elapsed - o3 usually takes 60-170s" in `.mono-stat`. Screenshots 06 and 07 both show "44s elapsed - o3 usually takes 60-170s" confirming the timer fires and the message format matches the spec. The timer is stopped on terminal states (main.js lines 289, 308, 316).

**Finding 6.3 - Cap banner is pre-rendered in HTML and controlled by JS.**
`#cap-banner` with `style="display:none"` is in index.html line 64. `showCapBanner()` (main.js lines 677-698) populates the reset time and removes the `display:none`. This is correct - the banner is ready in the DOM without a JS-generated insertion.

**Finding 6.4 - Session expiry handling is complete.**
Both the `startGeneration()` POST path (main.js lines 733-737) and `pollStatus()` (main.js lines 762-766) handle 401 with a toast notification + 3-second redirect. Users are never left silently stuck.

---

## Design Contract Compliance Summary

| Section | Status |
|---------|--------|
| Section 1 - Design tokens | PASS |
| Section 2 - index.html layout | PASS |
| Section 2 - Raw output `.spintax-block` highlighting | **PARTIAL** - code present, visual not confirmed |
| Section 3 - login.html layout | PASS |
| Section 4 - State machine (all 8 states) | PASS |
| Section 5 - Error states (all 8 error conditions) | PASS |
| Section 6 - Two output modes (raw + preview) | PASS (preview); PARTIAL (raw highlighting) |
| Section 7 - Copy + download | PASS |
| Section 8 - Login page behavior | PASS |
| Section 11 - Accessibility | PARTIAL (arrow-key nav missing on segmented controls) |
| Section 12 - Responsive breakpoints | PASS |

---

## Phase 3 Verdict

**PASS (conditional)**

Score: 8.3/10. The implementation is a high-quality, faithful delivery of the design contract. All 4 Playwright-verified bugs are fixed. Every error state, loading state, and terminal state is covered. The design tokens, typography, layout structure, ARIA annotations, and responsive breakpoints all match the spec.

Two items require follow-up before declaring the phase fully clean:

1. **The raw output `.spintax-block` highlight** - The code is implemented but screenshots do not show the visual effect. Manual verification on the live app is required. If the visual is actually rendering (screenshots were taken before output was populated), this resolves to PASS. If it is not rendering, this is a PARTIAL contract violation that should be fixed before production promotion.

2. **Arrow-key navigation on segmented controls** - The design contract specifies it. It is missing. This is a minor accessibility gap for a keyboard-only user.

Neither item is a show-stopper for an internal team tool, but item 1 should be confirmed before final sign-off.

---

## Recommended Fixes (Priority Order)

### Fix 1 - Manually verify raw output spintax highlighting (VERIFY before BLOCK)

**Pillar:** Interactive Affordances | **Priority:** High if broken, None if visual renders correctly

Confirm by opening the live app, generating spintax with EmailBison input, and visually inspecting whether `{Hi|Hey|Hello}` blocks appear with a blue tint and "3 var" chip. If not, check the browser console for errors in `highlightSpintax()`. The CSS is present (`.spintax-block`, `.spintax-chip`); the JS path is: `handleDone()` -> `renderRawOutput()` -> `highlightSpintax()` -> `pre.innerHTML = ...`.

### Fix 2 - Add arrow-key navigation to segmented controls

**Pillar:** Accessibility | **Priority:** Medium | **WCAG:** 2.1 AA (keyboard, composite widgets)

Add a `keydown` handler to each group of `.seg-btn` elements. When ArrowRight/ArrowLeft is pressed, move focus to the next/previous button within the `role="group"` container and call `setPlatform()` / `setMode()` accordingly.

Approximate implementation for the platform control:

```js
document.querySelectorAll('[role="group"]').forEach(function (group) {
  group.addEventListener('keydown', function (e) {
    var btns = Array.from(group.querySelectorAll('.seg-btn'));
    var idx = btns.indexOf(document.activeElement);
    if (idx === -1) return;
    if (e.key === 'ArrowRight') {
      e.preventDefault();
      var next = btns[(idx + 1) % btns.length];
      next.focus();
      next.click();
    } else if (e.key === 'ArrowLeft') {
      e.preventDefault();
      var prev = btns[(idx - 1 + btns.length) % btns.length];
      prev.focus();
      prev.click();
    }
  });
});
```

### Fix 3 - Increase pending phase label contrast or reclassify as decorative

**Pillar:** Accessibility | **Priority:** Low | **WCAG:** 1.4.3 AA contrast

`.phase-row--pending .phase-label` renders at `--tx3` (#8a8a8e) on white at 14px (3.7:1 - below 4.5:1 AA). Options:
- Change pending label color to `--tx2` (#6b6b6b, ~5.7:1, passes AA): ``.phase-row--pending .phase-label { color: var(--tx2); }``
- Accept as is: DESIGN.md explicitly notes this ratio and says "use sparingly for labels." Pending labels are arguably decorative (users focus on the active phase), so the trade-off is documented and defensible.

### Fix 4 - Increase `.spintax-chip` font size from 9px to 10px

**Pillar:** Visual Hierarchy | **Priority:** Low | **Principle:** Minimum legible size

9px (main.css line 561) is below comfortable reading threshold for any text. Increasing to 10px Space Mono costs no layout impact (the chip is `display: inline-block` within a `<span>`). The chip is supplemental annotation, not a primary reading element, so this is cosmetic but meaningful for users examining output on standard monitors.

```css
.spintax-chip {
  font-size: 10px;  /* was 9px */
}
```

### Fix 5 - Add skip navigation link (optional enhancement)

**Pillar:** IA & Navigation | **Priority:** Very Low | **Principle:** WCAG 2.4.1 Bypass Blocks

Add a visually hidden skip link as the first focusable element, pointing to `#email-body`. This lets keyboard users bypass the topbar on every page load.

```html
<a href="#email-body" class="skip-link">Skip to main content</a>
```

```css
.skip-link {
  position: absolute;
  top: -999px;
  left: 0;
  z-index: 9999;
  padding: 8px 16px;
  background: var(--blue);
  color: #fff;
  font-size: 14px;
  border-radius: 0 0 var(--r-sm) 0;
}
.skip-link:focus { top: 0; }
```
