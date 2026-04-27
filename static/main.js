/* Prospeqt Spintax Generator - main.js
 *
 * What this does:
 *   Drives the index.html UI: paste handling, platform selection,
 *   POST /api/spintax + 2-second poll loop on GET /api/status/{job_id},
 *   state-driven UI updates, raw/preview output toggle, copy/download.
 *
 * State machine (see DESIGN.md section 4):
 *   idle -> queued -> drafting -> linting -> iterating -> qa -> done | failed
 *
 * Critical rule: spintax_body from the API is set via element.textContent
 * or element.innerHTML (with explicit escaping) - never through Jinja.
 */

(function () {
  'use strict';

  // -----------------------------------------------------------------
  // Global state (window-scoped per DESIGN.md section 10)
  // -----------------------------------------------------------------
  window._jobId = null;
  window._pollInterval = null;
  window._rawSpintax = null;
  window._platform = 'instantly';
  window._mode = 'raw';
  window._model = 'o3';
  window._qaResult = null;
  window._startTime = null;
  window._elapsedTimer = null;
  window._iterationCount = 0;
  window._phaseStartTimes = {};

  // -----------------------------------------------------------------
  // Constants
  // -----------------------------------------------------------------
  var POLL_INTERVAL_MS = 2000;
  var ELAPSED_TICK_MS = 1000;
  var TERMINAL_STATES = ['done', 'failed'];

  // SVG icons for phase rows
  var ICON_CHECK = '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="3 8 7 12 13 4"/></svg>';
  var ICON_X = '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="4" y1="4" x2="12" y2="12"/><line x1="12" y1="4" x2="4" y2="12"/></svg>';
  var ICON_WARN = '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 1l7 13H1z"/><line x1="8" y1="6" x2="8" y2="9"/><circle cx="8" cy="11.5" r="0.5" fill="currentColor"/></svg>';

  // -----------------------------------------------------------------
  // Utilities
  // -----------------------------------------------------------------
  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function countWords(text) {
    if (!text) return 0;
    var trimmed = text.trim();
    if (!trimmed) return 0;
    return trimmed.split(/\s+/).filter(function (w) { return w.length > 0; }).length;
  }

  function setDataState(state) {
    var progressEl = document.getElementById('progress');
    if (progressEl) progressEl.dataset.state = state;
    document.body.dataset.state = state;
  }

  // -----------------------------------------------------------------
  // Word count
  // -----------------------------------------------------------------
  function updateInputWordCount() {
    var ta = document.getElementById('email-body');
    var wc = document.getElementById('word-count');
    if (!ta || !wc) return;
    var n = countWords(ta.value);
    wc.textContent = n + ' word' + (n === 1 ? '' : 's');
  }

  function updateOutputWordCount() {
    var wc = document.getElementById('output-word-count');
    if (!wc) return;
    var text = window._rawSpintax || '';
    var n = countWords(text);
    wc.textContent = n + ' word' + (n === 1 ? '' : 's');
  }

  // -----------------------------------------------------------------
  // Platform selection
  // -----------------------------------------------------------------
  function setPlatform(platform) {
    window._platform = platform;
    var btns = document.querySelectorAll('[data-action="select-platform"]');
    btns.forEach(function (b) {
      var active = b.dataset.platform === platform;
      b.classList.toggle('seg-btn--active', active);
      b.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
    // If output is showing, re-render with new platform
    if (window._rawSpintax) {
      renderRawOutput();
      if (window._mode === 'preview') randomizePreview();
    }
  }
  window.setPlatform = setPlatform;

  // -----------------------------------------------------------------
  // Input error
  // -----------------------------------------------------------------
  function showInputError() {
    var err = document.getElementById('input-error');
    var ta = document.getElementById('email-body');
    if (err) err.style.display = 'flex';
    if (ta) ta.classList.add('error');
  }

  function hideInputError() {
    var err = document.getElementById('input-error');
    var ta = document.getElementById('email-body');
    if (err) err.style.display = 'none';
    if (ta) ta.classList.remove('error');
  }

  // -----------------------------------------------------------------
  // Generate button loading + empty-input gating (BUG-02)
  // -----------------------------------------------------------------
  // setGenerateButtonLoading toggles the spinner / disabled state for
  // the duration of an in-flight job. updateGenerateButtonEnabled is a
  // separate concern: it disables the button whenever the textarea is
  // empty, regardless of generation state. Both must agree to enable
  // the button (loading=false AND textarea has content).
  function setGenerateButtonLoading(loading) {
    var btn = document.getElementById('generate-btn');
    var ta = document.getElementById('email-body');
    if (!btn) return;
    if (loading) {
      btn.dataset.loading = 'true';
      btn.disabled = true;
      btn.innerHTML = '<span class="spinner-white"></span>Generating...';
      if (ta) ta.disabled = true;
    } else {
      btn.dataset.loading = 'false';
      btn.textContent = 'Generate spintax';
      if (ta) ta.disabled = false;
      // Honour the empty-input gate when leaving the loading state.
      updateGenerateButtonEnabled();
    }
  }

  // Mirrors the textarea's emptiness onto the Generate button's
  // disabled attribute. Called on input events and on page load.
  // Skipped while a generation is in flight (loading wins).
  function updateGenerateButtonEnabled() {
    var btn = document.getElementById('generate-btn');
    var ta = document.getElementById('email-body');
    if (!btn || !ta) return;
    if (btn.dataset.loading === 'true') return;
    var hasText = ta.value.trim() !== '';
    btn.disabled = !hasText;
  }

  // -----------------------------------------------------------------
  // Phase rows (progress list)
  // -----------------------------------------------------------------
  var PHASES = [
    { key: 'queued',    label: 'Queued' },
    { key: 'drafting',  label: 'Drafting' },
    { key: 'linting',   label: 'Linting' },
    { key: 'iterating', label: 'Iteration' },
    { key: 'qa',        label: 'Running QA' }
  ];

  function renderPhaseRow(phase, status, opts) {
    opts = opts || {};
    var iconHtml;
    if (status === 'active') {
      iconHtml = '<span class="spinner"></span>';
    } else if (status === 'done') {
      iconHtml = ICON_CHECK;
    } else if (status === 'failed') {
      iconHtml = ICON_X;
    } else {
      iconHtml = ''; // pending - styled via ::before
    }
    var label = opts.label || phase.label;
    var duration = opts.duration ? '<span class="phase-duration">' + escapeHtml(opts.duration) + '</span>' : '';
    return '<div class="phase-row phase-row--' + status + '" data-phase="' + escapeHtml(phase.key) + '">' +
      '<span class="phase-icon">' + iconHtml + '</span>' +
      '<span class="phase-label">' + escapeHtml(label) + '</span>' +
      duration +
      '</div>';
  }

  function renderProgressList(currentState, data) {
    var list = document.getElementById('progress-list');
    if (!list) return;
    data = data || {};

    var stateOrder = ['queued', 'drafting', 'linting', 'iterating', 'qa'];
    var currentIdx = stateOrder.indexOf(currentState);
    if (currentState === 'done') currentIdx = stateOrder.length;
    if (currentState === 'failed') currentIdx = -1;

    var html = '';
    var iterationCount = 0;
    if (data.progress && typeof data.progress.iteration_count === 'number') {
      iterationCount = data.progress.iteration_count;
    }
    if (currentState === 'iterating' && iterationCount === 0) {
      iterationCount = 1;
    }

    PHASES.forEach(function (p, i) {
      var label = p.label;
      if (p.key === 'iterating') {
        label = iterationCount > 0 ? ('Iteration ' + iterationCount + ' - refining blocks') : 'Iteration';
        if (currentState !== 'iterating' && currentState !== 'done' && currentIdx < i) {
          // Skip iteration row if we never entered it and are still earlier
          return;
        }
        if (currentState === 'done' && iterationCount === 0) {
          // No iterations needed; skip the row entirely
          return;
        }
      }

      var status;
      if (currentState === 'done') {
        status = 'done';
      } else if (currentState === 'failed') {
        // Phases up to current are done, current is failed
        var failedAtIdx = stateOrder.indexOf(data._failedPhase || 'drafting');
        if (failedAtIdx === -1) failedAtIdx = stateOrder.length - 1;
        if (i < failedAtIdx) status = 'done';
        else if (i === failedAtIdx) status = 'failed';
        else status = 'pending';
      } else {
        if (currentIdx === -1) status = 'pending';
        else if (i < currentIdx) status = 'done';
        else if (i === currentIdx) status = 'active';
        else status = 'pending';
      }

      html += renderPhaseRow(p, status, { label: label });
    });

    list.innerHTML = html;
  }

  // -----------------------------------------------------------------
  // Elapsed time (ETA after 30s)
  // -----------------------------------------------------------------
  function updateElapsedTime() {
    var meta = document.getElementById('progress-meta');
    if (!meta || !window._startTime) return;
    var elapsed = Math.floor((Date.now() - window._startTime) / 1000);
    if (elapsed < 30) {
      meta.textContent = '';
      return;
    }
    var m = window._model || 'o3';
    var hint = m === 'o4-mini' ? '30-80s' : m === 'o3-pro' ? '120-300s' : '60-170s';
    meta.innerHTML = '<span class="mono-stat">' + elapsed + 's elapsed - ' + escapeHtml(m) + ' usually takes ' + hint + '</span>';
  }

  function startElapsedTimer() {
    stopElapsedTimer();
    window._elapsedTimer = setInterval(updateElapsedTime, ELAPSED_TICK_MS);
  }

  function stopElapsedTimer() {
    if (window._elapsedTimer) {
      clearInterval(window._elapsedTimer);
      window._elapsedTimer = null;
    }
  }

  // -----------------------------------------------------------------
  // State dispatcher (DESIGN.md section 10)
  // -----------------------------------------------------------------
  function setState(state, data) {
    data = data || {};
    setDataState(state);

    var progress = document.getElementById('progress');
    var output = document.getElementById('output');

    switch (state) {
      case 'idle':
        if (progress) progress.style.display = 'none';
        if (output) output.style.display = 'none';
        stopElapsedTimer();
        break;

      case 'queued':
      case 'drafting':
      case 'linting':
      case 'iterating':
      case 'qa':
        if (progress) progress.style.display = '';
        if (output) output.style.display = 'none';
        renderProgressList(state, data);
        clearErrorCard();
        break;

      case 'done':
        if (progress) progress.style.display = '';
        renderProgressList('done', data);
        clearErrorCard();
        handleDone(data);
        stopElapsedTimer();
        break;

      case 'failed':
        if (progress) progress.style.display = '';
        renderProgressList('failed', data);
        handleFailed(data);
        if (output) output.style.display = 'none';
        stopElapsedTimer();
        break;
    }
  }

  // -----------------------------------------------------------------
  // Done state - render output
  // -----------------------------------------------------------------
  function handleDone(data) {
    var result = data.result || {};
    window._rawSpintax = result.spintax_body || '';
    window._qaResult = result.qa || null;

    renderRawOutput();
    renderOutputBadges(result, data);
    populateVariantPicker();

    var output = document.getElementById('output');
    if (output) output.style.display = '';

    updateOutputWordCount();

    // Default mode is raw
    setMode('raw');

    // T8: lint pass + qa fail = toast warning
    var qaPassed = result.qa && result.qa.passed === true;
    if (!qaPassed) {
      showToast('QA found minor issues - click the QA badge to see details', 'warn');
    }
  }

  function renderRawOutput() {
    var pre = document.getElementById('raw-output');
    if (!pre) return;
    var text = window._rawSpintax || '';
    var platform = window._platform;
    pre.innerHTML = highlightSpintax(text, platform);
  }

  function renderOutputBadges(result, data) {
    var box = document.getElementById('output-badges');
    if (!box) return;
    var lintPassed = result.lint && result.lint.passed === true;
    var qaPassed = result.qa && result.qa.passed === true;
    var html = '';
    html += '<span class="badge ' + (lintPassed ? 'badge-green' : 'badge-red') + '">Lint ' + (lintPassed ? 'PASS' : 'FAIL') + '</span>';
    if (qaPassed) {
      html += '<span class="badge badge-green">QA PASS</span>';
    } else {
      html += '<button type="button" class="badge badge-amber badge-btn" onclick="toggleQAPanel(event, this)" aria-haspopup="true" aria-expanded="false">QA issues &darr;</button>';
    }
    var costParts = [];
    if (typeof result.cost_usd === 'number') costParts.push('$' + result.cost_usd.toFixed(2));
    if (typeof result.api_calls === 'number') costParts.push(result.api_calls + ' calls');
    if (typeof data.elapsed_sec === 'number') costParts.push(Math.round(data.elapsed_sec) + 's');
    if (costParts.length) {
      html += '<span class="badge badge-info mono">' + escapeHtml(costParts.join(' - ')) + '</span>';
    }
    box.innerHTML = html;
    // Clear the QA panel slot whenever badges are re-rendered
    var slot = document.getElementById('qa-panel-slot');
    if (slot) slot.innerHTML = '';
  }

  function toggleQAPanel(event, btn) {
    event.stopPropagation();
    var slot = document.getElementById('qa-panel-slot');
    if (!slot) return;
    var isOpen = btn.getAttribute('aria-expanded') === 'true';
    if (isOpen) {
      btn.setAttribute('aria-expanded', 'false');
      btn.textContent = 'QA issues ↓';
      slot.innerHTML = '';
      return;
    }
    btn.setAttribute('aria-expanded', 'true');
    btn.textContent = 'QA issues ↑';
    var qa = window._qaResult;
    var errors = (qa && qa.errors) ? qa.errors : [];
    var warnings = (qa && qa.warnings) ? qa.warnings : [];
    if (!errors.length && !warnings.length) {
      slot.innerHTML = '<div class="qa-panel"><p class="qa-panel-empty">No specific details were recorded.</p></div>';
    } else {
      var html = '<div class="qa-panel">';
      if (errors.length) {
        html += '<div class="qa-panel-section"><span class="qa-panel-label qa-panel-label--error">Errors</span>';
        errors.forEach(function (e) {
          html += '<div class="qa-panel-item qa-panel-item--error">' + escapeHtml(e) + '</div>';
        });
        html += '</div>';
      }
      if (warnings.length) {
        html += '<div class="qa-panel-section"><span class="qa-panel-label qa-panel-label--warn">Warnings</span>';
        warnings.forEach(function (w) {
          html += '<div class="qa-panel-item qa-panel-item--warn">' + escapeHtml(w) + '</div>';
        });
        html += '</div>';
      }
      html += '</div>';
      slot.innerHTML = html;
    }
    // Close on outside click
    setTimeout(function () {
      function onOutside(e) {
        if (!slot.contains(e.target) && e.target !== btn) {
          btn.setAttribute('aria-expanded', 'false');
          btn.textContent = 'QA issues ↓';
          slot.innerHTML = '';
          document.removeEventListener('click', onOutside);
        }
      }
      document.addEventListener('click', onOutside);
    }, 0);
  }
  window.toggleQAPanel = toggleQAPanel;

  // -----------------------------------------------------------------
  // Failed state - render error card
  // -----------------------------------------------------------------
  function handleFailed(data) {
    var slot = document.getElementById('error-card-slot');
    if (!slot) return;
    var errorKey = data.error || data.message || 'unknown';
    var info = errorInfo(errorKey);
    var retryHtml = info.retry
      ? '<button class="btn btn-ghost btn-sm" data-action="retry" onclick="retryGeneration()">Retry</button>'
      : '';
    slot.innerHTML =
      '<div class="error-card" role="alert">' +
        '<p class="error-title">' + escapeHtml(info.title) + '</p>' +
        '<p class="error-detail">' + escapeHtml(info.detail) + '</p>' +
        retryHtml +
      '</div>';
  }

  function clearErrorCard() {
    var slot = document.getElementById('error-card-slot');
    if (slot) slot.innerHTML = '';
  }

  function errorInfo(key) {
    switch (key) {
      case 'openai_timeout':
        return { title: 'Generation timed out', detail: 'The model took too long to respond.', retry: true };
      case 'openai_quota':
        return { title: 'OpenAI quota hit', detail: 'Ping Mihajlo to top up.', retry: false };
      case 'max_tool_calls':
        return { title: 'Could not converge', detail: 'Linting could not pass in 10 iterations. Retry or simplify the input.', retry: true };
      case 'malformed_response':
        return { title: 'Unexpected output', detail: 'The model returned a malformed response. Retry?', retry: true };
      case 'job_not_found':
        return { title: 'Result lost', detail: 'Generation result was not found. Retry?', retry: true };
      case 'submit_failed':
        return { title: 'Could not start generation', detail: 'The server rejected the request. Retry?', retry: true };
      case 'network_error':
        return { title: 'Network error', detail: 'Check your connection and retry.', retry: true };
      default:
        return { title: 'Generation failed', detail: 'Something went wrong. Retry?', retry: true };
    }
  }

  // -----------------------------------------------------------------
  // Spintax tokenizer - shared by highlightSpintax and randomize
  // -----------------------------------------------------------------
  // Walks the body left-to-right and splits it into a sequence of
  // {kind, raw, parts} tokens. Two block kinds are recognized as
  // randomizable spintax:
  //   - "double": {{RANDOM | a | b | c }}        Instantly format
  //   - "single": {a|b|c}                        EmailBison format
  // Plus three pass-through kinds:
  //   - "variable": {{firstName}}                Double-brace variable
  //   - "static_single": {someText}              Single-brace token without `|`
  //   - "text": anything else                    Plain text run
  //
  // Variables (`{{firstName}}`) and option-less single-brace tokens
  // (`{username}`) are NEVER resolved - they pass through unchanged.
  //
  // findDoubleClose skips nested {{...}} pairs so that a RANDOM block
  // whose options contain variables like {{firstName}} is not cut short
  // at the first `}}` it encounters (which belongs to the nested var).
  function findDoubleClose(text, start) {
    var j = start + 2; // skip the opening {{
    var len = text.length;
    while (j < len) {
      if (text.charCodeAt(j) === 123 && j + 1 < len && text.charCodeAt(j + 1) === 123) {
        // Nested {{ — recurse to find its matching }}
        var nestedEnd = findDoubleClose(text, j);
        if (nestedEnd === -1) return -1;
        j = nestedEnd + 2;
      } else if (text.charCodeAt(j) === 125 && j + 1 < len && text.charCodeAt(j + 1) === 125) {
        return j; // found the real closing }}
      } else {
        j++;
      }
    }
    return -1; // unclosed
  }

  // splitOptions splits an options string by top-level `|` only,
  // ignoring `|` that appear inside nested {{...}} blocks.
  function splitOptions(str) {
    var parts = [];
    var buf = '';
    var j = 0;
    var len = str.length;
    while (j < len) {
      if (str.charCodeAt(j) === 123 && j + 1 < len && str.charCodeAt(j + 1) === 123) {
        // Nested {{ — copy until its closing }}
        var nestedEnd = findDoubleClose(str, j);
        if (nestedEnd === -1) {
          buf += str.slice(j);
          break;
        }
        buf += str.slice(j, nestedEnd + 2);
        j = nestedEnd + 2;
      } else if (str.charCodeAt(j) === 124 /* | */) {
        parts.push(buf.trim());
        buf = '';
        j++;
      } else {
        buf += str.charAt(j);
        j++;
      }
    }
    parts.push(buf.trim());
    return parts;
  }

  function tokenizeSpintax(text) {
    var tokens = [];
    var i = 0;
    var n = text.length;
    var buf = '';

    function flushText() {
      if (buf) {
        tokens.push({ kind: 'text', raw: buf });
        buf = '';
      }
    }

    while (i < n) {
      var ch = text.charCodeAt(i);
      // Look for a brace block starting here.
      if (ch === 123 /* { */) {
        // Double-brace: {{...}}
        if (i + 1 < n && text.charCodeAt(i + 1) === 123) {
          var endDouble = findDoubleClose(text, i);
          if (endDouble !== -1) {
            var doubleRaw = text.slice(i, endDouble + 2);
            var doubleInner = text.slice(i + 2, endDouble);
            // Detect Instantly RANDOM block: starts with "RANDOM" then
            // optional whitespace then a pipe. Case-insensitive on the
            // RANDOM keyword to match what spintax_lint accepts.
            var randomMatch = /^RANDOM\s*\|/i.exec(doubleInner);
            if (randomMatch) {
              var afterPipe = doubleInner.slice(randomMatch[0].length);
              var parts = splitOptions(afterPipe);
              flushText();
              tokens.push({ kind: 'double', raw: doubleRaw, parts: parts });
            } else {
              // Plain double-brace variable like {{firstName}} - pass through
              flushText();
              tokens.push({ kind: 'variable', raw: doubleRaw });
            }
            i = endDouble + 2;
            continue;
          }
          // Unclosed {{ - fall through to character-level handling
        } else {
          // Single-brace block: {a|b|c} or {token}
          var endSingle = text.indexOf('}', i + 1);
          if (endSingle !== -1) {
            var singleRaw = text.slice(i, endSingle + 1);
            var singleInner = text.slice(i + 1, endSingle);
            // Reject if inner contains a `{` (avoid eating into an
            // adjacent `{{` like `{{firstName}}` if scanning got weird)
            if (singleInner.indexOf('{') === -1) {
              if (singleInner.indexOf('|') !== -1) {
                var singleParts = singleInner.split('|').map(function (s) { return s.trim(); });
                flushText();
                tokens.push({ kind: 'single', raw: singleRaw, parts: singleParts });
              } else {
                // Single-brace token without `|` - leave alone (some
                // platforms use `{username}` as a variable form too)
                flushText();
                tokens.push({ kind: 'static_single', raw: singleRaw });
              }
              i = endSingle + 1;
              continue;
            }
          }
        }
      }
      buf += text.charAt(i);
      i++;
    }
    flushText();
    return tokens;
  }

  // -----------------------------------------------------------------
  // Spintax highlighting + randomize (DESIGN.md section 6)
  // -----------------------------------------------------------------
  function highlightSpintax(text, platform) {
    if (!text) return '';
    // platform argument retained for signature stability; not used here
    // because tokenizeSpintax handles BOTH formats automatically.
    void platform;
    var tokens = tokenizeSpintax(text);
    var out = '';
    for (var i = 0; i < tokens.length; i++) {
      var t = tokens[i];
      if (t.kind === 'double' || t.kind === 'single') {
        out += '<span class="spintax-block" data-count="' + t.parts.length + '">' +
          escapeHtml(t.raw) +
          '<span class="spintax-chip">' + t.parts.length + ' var</span>' +
          '</span>';
      } else {
        out += escapeHtml(t.raw);
      }
    }
    return out;
  }

  // randomize: resolve every spintax block to one random variant.
  // Variables ({{firstName}}) and static single-brace tokens
  // ({username}) pass through unchanged. Each call picks fresh
  // random options (Math.random per block).
  function randomize(text, platform) {
    if (!text) return '';
    void platform; // accepted for signature compatibility
    var tokens = tokenizeSpintax(text);
    var out = '';
    for (var i = 0; i < tokens.length; i++) {
      var t = tokens[i];
      if ((t.kind === 'double' || t.kind === 'single') && t.parts && t.parts.length) {
        out += t.parts[Math.floor(Math.random() * t.parts.length)];
      } else {
        out += t.raw;
      }
    }
    return out;
  }

  function randomizePreview() {
    var out = document.getElementById('preview-output');
    if (!out) return;
    var text = window._rawSpintax;
    if (!text) {
      out.textContent = '';
      return;
    }
    out.textContent = randomize(text, window._platform);
  }
  window.randomizePreview = randomizePreview;

  // -----------------------------------------------------------------
  // Variant picker - deterministic preview by variant index
  // -----------------------------------------------------------------
  function resolveVariant(text, idx) {
    if (!text) return '';
    var tokens = tokenizeSpintax(text);
    return tokens.map(function (tok) {
      if ((tok.kind === 'double' || tok.kind === 'single') && tok.parts && tok.parts.length) {
        return tok.parts[Math.min(idx, tok.parts.length - 1)];
      }
      return tok.raw;
    }).join('');
  }

  function populateVariantPicker() {
    var picker = document.getElementById('variant-picker');
    if (!picker) return;
    var text = window._rawSpintax || '';
    var tokens = tokenizeSpintax(text);
    var minParts = Infinity;
    tokens.forEach(function (tok) {
      if ((tok.kind === 'double' || tok.kind === 'single') && tok.parts) {
        if (tok.parts.length < minParts) minParts = tok.parts.length;
      }
    });
    if (!isFinite(minParts) || minParts === 0) {
      picker.style.display = 'none';
      return;
    }
    var html = '<option value="">Preview variant</option>';
    for (var i = 0; i < minParts; i++) {
      html += '<option value="' + i + '">Variant ' + (i + 1) + '</option>';
    }
    picker.innerHTML = html;
    picker.value = '';
    picker.style.display = '';
  }

  function setVariant(val) {
    var rawContainer = document.getElementById('raw-container');
    var previewContainer = document.getElementById('preview-container');
    var rawBtn = document.getElementById('mode-raw');
    if (val === '' || val === null || val === undefined) {
      // Switch back to raw
      setMode('raw');
      return;
    }
    var idx = parseInt(val, 10);
    if (isNaN(idx)) return;
    window._mode = 'preview';
    if (rawContainer) rawContainer.style.display = 'none';
    if (previewContainer) previewContainer.style.display = '';
    if (rawBtn) {
      rawBtn.classList.remove('seg-btn--active');
      rawBtn.setAttribute('aria-pressed', 'false');
    }
    var out = document.getElementById('preview-output');
    if (out) out.textContent = resolveVariant(window._rawSpintax || '', idx);
    updateOutputWordCount();
  }
  window.setVariant = setVariant;

  // -----------------------------------------------------------------
  // Model picker
  // -----------------------------------------------------------------
  function setModelPicker(val) {
    window._model = val || 'o3';
  }
  window.setModelPicker = setModelPicker;

  // -----------------------------------------------------------------
  // Batch mode (multi-segment .md flow)
  // -----------------------------------------------------------------
  // Global batch state. Initialized lazily so single-mode users never pay.
  window._batchMode = false;
  window._batchId = null;
  window._batchPollInterval = null;
  var BATCH_POLL_MS = 5000;

  // Heuristic detector: does the input look like a multi-segment .md?
  function detectIsBatch(text) {
    if (!text) return false;
    // # Segment 1, ## Segment N, etc.
    if (/^#+\s+segment\s/im.test(text)) return true;
    // # Copy Agencies / # Copy Sales teams pattern
    if (/^#+\s+copy\s+/im.test(text)) return true;
    // Email 1 + Email 2 markers in the same doc
    if (/^email\s+1\b/im.test(text) && /^email\s+2\b/im.test(text)) return true;
    return false;
  }

  function updateModeIndicator() {
    var ta = document.getElementById('email-body');
    var ind = document.getElementById('mode-indicator');
    var btn = document.getElementById('generate-btn');
    var concWrap = document.getElementById('concurrency-wrap');
    if (!ta) return;

    var text = ta.value;
    var isBatch = detectIsBatch(text);
    window._batchMode = isBatch;

    if (concWrap) concWrap.style.display = isBatch ? '' : 'none';

    if (ind) {
      if (!text.trim()) {
        ind.textContent = '';
        ind.style.display = 'none';
        ind.removeAttribute('data-mode');
      } else if (isBatch) {
        ind.textContent = 'Multi-segment .md detected';
        ind.dataset.mode = 'batch';
        ind.style.display = '';
      } else {
        ind.textContent = 'Single email mode';
        ind.dataset.mode = 'single';
        ind.style.display = '';
      }
    }

    if (btn) {
      btn.textContent = isBatch ? 'Parse markdown' : 'Generate spintax';
    }
  }

  function handleMdFileSelect(event) {
    var f = event.target.files && event.target.files[0];
    if (!f) return;
    var nameEl = document.getElementById('md-filename');
    if (nameEl) nameEl.textContent = f.name;

    var reader = new FileReader();
    reader.onload = function () {
      var ta = document.getElementById('email-body');
      if (ta) {
        ta.value = String(reader.result || '');
        updateInputWordCount();
        updateGenerateButtonEnabled();
        updateModeIndicator();
      }
    };
    reader.onerror = function () {
      showToast('Could not read file: ' + f.name, 'warn');
    };
    reader.readAsText(f);
  }
  window.handleMdFileSelect = handleMdFileSelect;

  // ---- Batch flow: parse (dry-run) -> confirm -> fire -> poll -> done ----

  function showBatchSection(stage) {
    // stage in {'input', 'parsing', 'confirm', 'progress'}
    // Keeps the existing single-mode sections in mind: when batch mode is
    // active, we hide single-mode progress/output and show batch ones.
    var inputSec = document.getElementById('input-section');
    var batchParse = document.getElementById('batch-parsing');
    var batchConfirm = document.getElementById('batch-confirm');
    var batchProg = document.getElementById('batch-progress');
    var singleProg = document.getElementById('progress');
    var singleOut = document.getElementById('output');

    if (singleProg) singleProg.style.display = 'none';
    if (singleOut) singleOut.style.display = 'none';

    if (inputSec) inputSec.style.display = (stage === 'input') ? '' : 'none';
    if (batchParse) batchParse.style.display = (stage === 'parsing') ? '' : 'none';
    if (batchConfirm) batchConfirm.style.display = (stage === 'confirm') ? '' : 'none';
    if (batchProg) batchProg.style.display = (stage === 'progress') ? '' : 'none';
  }

  async function startBatchParse() {
    var ta = document.getElementById('email-body');
    var md = ta ? ta.value.trim() : '';
    if (!md) {
      showInputError();
      return;
    }
    hideInputError();
    showBatchSection('parsing');

    try {
      var concEl = document.getElementById('batch-concurrency');
      var concurrency = concEl ? parseInt(concEl.value, 10) || 5 : 5;
      var resp = await fetch('/api/spintax/batch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          md: md,
          platform: window._platform,
          model: window._model || 'o3',
          concurrency: concurrency,
          dry_run: true
        })
      });

      if (resp.status === 401) {
        showToast('Session expired. Redirecting...', 'warn');
        setTimeout(function () { window.location.href = '/login'; }, 2000);
        return;
      }

      var data = null;
      try { data = await resp.json(); } catch (e) { /* swallow */ }

      if (!resp.ok) {
        var msg = (data && (data.detail || data.message)) ||
                  ('Parse failed (' + resp.status + ')');
        if (typeof msg === 'object') msg = JSON.stringify(msg);
        showToast(String(msg), 'warn');
        showBatchSection('input');
        return;
      }

      window._batchId = data.batch_id;
      renderBatchConfirm(data);
      showBatchSection('confirm');
    } catch (err) {
      showToast('Network error: ' + err.message, 'warn');
      showBatchSection('input');
    }
  }
  window.startBatchParse = startBatchParse;

  function renderBatchConfirm(data) {
    var p = data.parsed || {};
    var totalBodies = p.total_bodies || 0;
    var toSpin = (typeof p.total_bodies_to_spin === 'number')
      ? p.total_bodies_to_spin : totalBodies;
    var passthrough = totalBodies - toSpin;

    var stat = document.getElementById('batch-confirm-stat');
    if (stat) {
      var statText = (p.segments || []).length + ' segments · ' +
                     totalBodies + ' bodies';
      if (passthrough > 0) {
        statText += ' (' + toSpin + ' to spin · ' + passthrough +
                    ' Email 2 pass through)';
      }
      stat.textContent = statText;
    }
    var cc = document.getElementById('confirm-count');
    if (cc) cc.textContent = String(toSpin);

    var warnEl = document.getElementById('batch-warnings');
    if (warnEl) {
      if (p.warnings && p.warnings.length) {
        var html = '<strong>Warnings:</strong><ul>';
        p.warnings.forEach(function (w) {
          html += '<li>' + escapeHtml(w) + '</li>';
        });
        html += '</ul>';
        warnEl.innerHTML = html;
        warnEl.style.display = '';
      } else {
        warnEl.style.display = 'none';
      }
    }

    // Group by section
    var bySection = {};
    var orderedSections = [];
    (p.segments || []).forEach(function (s) {
      var sec = s.section || '(no section)';
      if (!(sec in bySection)) {
        bySection[sec] = [];
        orderedSections.push(sec);
      }
      bySection[sec].push(s);
    });

    function segRowHtml(s, ordinal) {
      var warns = (s.warnings && s.warnings.length)
        ? '<span class="batch-seg-warn">⚠ ' + escapeHtml(s.warnings.join(', ')) + '</span>'
        : '';
      return '<li class="batch-seg-row">' +
             '  <span class="batch-seg-num">' + String(ordinal).padStart(2, '0') + '</span>' +
             '  <span class="batch-seg-name">' + escapeHtml(s.name) + '</span>' +
             '  <span class="batch-seg-emails">' + s.email_count + ' ✉</span>' +
             warns +
             '</li>';
    }

    var listHtml = '';
    if (orderedSections.length <= 1) {
      // Single-section docs (e.g., Enavra) — render flat list. The section
      // grouping was confusing users into thinking the section header was
      // a single segment.
      listHtml = '<ol class="batch-segment-rows batch-segment-rows--flat">';
      var segs0 = bySection[orderedSections[0]] || [];
      segs0.forEach(function (s, i) {
        listHtml += segRowHtml(s, i + 1);
      });
      listHtml += '</ol>';
    } else {
      // Multi-section docs — keep section grouping with row numbering
      // continuing across sections so the eye sees "01..N segments".
      var ordinal = 1;
      orderedSections.forEach(function (sec) {
        var segs = bySection[sec];
        var bodies = segs.reduce(function (a, s) { return a + s.email_count; }, 0);
        listHtml += '<div class="batch-section-group">';
        listHtml += '  <div class="batch-section-header">' + escapeHtml(sec) +
                    ' <span class="batch-section-meta">(' + segs.length + ' segments · ' +
                    bodies + ' bodies)</span></div>';
        listHtml += '  <ol class="batch-segment-rows">';
        segs.forEach(function (s) {
          listHtml += segRowHtml(s, ordinal);
          ordinal++;
        });
        listHtml += '  </ol>';
        listHtml += '</div>';
      });
    }
    var listEl = document.getElementById('batch-segment-list');
    if (listEl) listEl.innerHTML = listHtml;
  }

  function resetBatchToInput() {
    if (window._batchPollInterval) {
      clearInterval(window._batchPollInterval);
      window._batchPollInterval = null;
    }
    window._batchId = null;
    showBatchSection('input');
  }
  window.resetBatchToInput = resetBatchToInput;

  async function fireBatch() {
    var ta = document.getElementById('email-body');
    var md = ta ? ta.value.trim() : '';
    if (!md) return;

    // Immediate feedback before fetch returns.
    var btn = document.getElementById('confirm-btn');
    if (btn) {
      btn.disabled = true;
      btn.dataset.origLabel = btn.innerHTML;
      btn.innerHTML = '<span class="spinner-white"></span>Submitting...';
    }
    showBatchSubmittingProgress();
    showBatchSection('progress');

    try {
      var concEl = document.getElementById('batch-concurrency');
      var concurrency = concEl ? parseInt(concEl.value, 10) || 5 : 5;
      var resp = await fetch('/api/spintax/batch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          md: md,
          platform: window._platform,
          model: window._model || 'o3',
          concurrency: concurrency,
          dry_run: false
        })
      });

      if (resp.status === 401) {
        showToast('Session expired. Redirecting...', 'warn');
        setTimeout(function () { window.location.href = '/login'; }, 2000);
        return;
      }

      var data = null;
      try { data = await resp.json(); } catch (e) { /* swallow */ }

      if (!resp.ok) {
        var msg = (data && (data.detail || data.message)) ||
                  ('Batch submit failed (' + resp.status + ')');
        if (typeof msg === 'object') msg = JSON.stringify(msg);
        showToast(String(msg), 'warn');
        if (btn) {
          btn.disabled = false;
          if (btn.dataset.origLabel) btn.innerHTML = btn.dataset.origLabel;
        }
        showBatchSection('confirm');
        return;
      }

      window._batchId = data.batch_id;
      renderBatchProgress({
        batch_id: data.batch_id,
        status: 'running',
        completed: 0, failed: 0, in_progress: 0, retrying: 0,
        queued: data.total_jobs, total: data.total_jobs,
        retries_used: 0, elapsed_sec: 0,
        cost_usd_so_far: 0, cost_usd_estimated_total: 0,
        failure_reason: null, download_url: null,
        parsed: data.parsed
      });
      startBatchPolling();
    } catch (err) {
      showToast('Network error: ' + err.message, 'warn');
      if (btn) {
        btn.disabled = false;
        if (btn.dataset.origLabel) btn.innerHTML = btn.dataset.origLabel;
      }
      showBatchSection('confirm');
    }
  }
  window.fireBatch = fireBatch;

  function showBatchSubmittingProgress() {
    var titleEl = document.getElementById('batch-progress-title');
    var metaEl = document.getElementById('batch-progress-meta');
    var statsEl = document.getElementById('batch-progress-stats');
    var actionsEl = document.getElementById('batch-progress-actions');
    var bar = document.getElementById('batch-progress-bar');
    if (titleEl) titleEl.textContent = 'Submitting batch...';
    if (metaEl) metaEl.textContent = 'Firing OpenAI jobs...';
    if (statsEl) statsEl.innerHTML = '<span class="batch-stat">Submitting...</span>';
    if (actionsEl) actionsEl.innerHTML = '';
    if (bar) {
      bar.style.width = '5%';
      bar.dataset.status = 'running';
    }
  }

  function startBatchPolling() {
    if (window._batchPollInterval) clearInterval(window._batchPollInterval);
    window._batchPollInterval = setInterval(pollBatch, BATCH_POLL_MS);
    pollBatch();
  }

  function stopBatchPolling() {
    if (window._batchPollInterval) {
      clearInterval(window._batchPollInterval);
      window._batchPollInterval = null;
    }
  }

  async function pollBatch() {
    if (!window._batchId) return;
    try {
      var resp = await fetch('/api/spintax/batch/' +
        encodeURIComponent(window._batchId));
      if (resp.status === 401) {
        stopBatchPolling();
        showToast('Session expired. Redirecting...', 'warn');
        setTimeout(function () { window.location.href = '/login'; }, 2000);
        return;
      }
      if (resp.status === 404) {
        stopBatchPolling();
        showToast('Batch not found (may have expired)', 'warn');
        return;
      }
      if (!resp.ok) return;  // transient
      var data = await resp.json();
      renderBatchProgress(data);
      if (data.status === 'done' || data.status === 'failed' ||
          data.status === 'cancelled') {
        stopBatchPolling();
      }
    } catch (err) {
      // network blip — keep polling
    }
  }

  function renderBatchProgress(data) {
    var titleEl = document.getElementById('batch-progress-title');
    var metaEl = document.getElementById('batch-progress-meta');
    var actionsEl = document.getElementById('batch-progress-actions');

    var done = (data.completed || 0) + (data.failed || 0);
    var pct = data.total > 0 ? Math.round((done / data.total) * 100) : 0;

    var statusText;
    if (data.status === 'done') {
      statusText = 'Done ✓';
    } else if (data.status === 'failed') {
      statusText = 'Batch failed: ' + (data.failure_reason || 'unknown');
    } else if (data.status === 'cancelled') {
      statusText = 'Cancelled';
    } else {
      statusText = 'Spinning - ' + done + ' of ' + data.total + ' (' + pct + '%)';
    }
    if (titleEl) titleEl.textContent = statusText;

    if (metaEl) {
      var parts = [];
      parts.push(formatBatchDuration(data.elapsed_sec || 0) + ' elapsed');
      parts.push('$' + (data.cost_usd_so_far || 0).toFixed(2) + ' spent');
      if (data.cost_usd_estimated_total) {
        parts.push('~$' + data.cost_usd_estimated_total.toFixed(2) + ' estimated total');
      }
      if (data.retries_used > 0) {
        parts.push(data.retries_used + ' retries');
      }
      metaEl.textContent = parts.join(' · ');
    }

    var bar = document.getElementById('batch-progress-bar');
    if (bar) {
      bar.style.width = pct + '%';
      bar.dataset.status = data.status;
    }

    var stats = document.getElementById('batch-progress-stats');
    if (stats) {
      stats.innerHTML =
        '<span class="batch-stat batch-stat--done"><strong>' + (data.completed || 0) + '</strong> done</span>' +
        '<span class="batch-stat batch-stat--running"><strong>' + (data.in_progress || 0) + '</strong> running</span>' +
        '<span class="batch-stat batch-stat--retry"><strong>' + (data.retrying || 0) + '</strong> retrying</span>' +
        '<span class="batch-stat"><strong>' + (data.queued || 0) + '</strong> queued</span>' +
        '<span class="batch-stat batch-stat--failed"><strong>' + (data.failed || 0) + '</strong> failed</span>';
    }

    if (actionsEl) {
      var html = '';
      if (data.status === 'running') {
        html += '<button class="btn btn-ghost" onclick="cancelBatchRun()">Cancel batch</button>';
      } else {
        html += '<button class="btn btn-ghost" onclick="resetBatchToInput()">New batch</button>';
        if (data.download_url) {
          html += '<a class="btn btn--full" href="' + escapeHtml(data.download_url) + '" download>Download .zip</a>';
        }
      }
      actionsEl.innerHTML = html;
    }

    var segWrap = document.getElementById('batch-segment-progress');
    if (segWrap && data.parsed) {
      var html2 = '';
      (data.parsed.segments || []).forEach(function (s, i) {
        html2 += '<div class="batch-prog-seg">' +
                 '  <span class="batch-prog-seg-num">' + String(i + 1).padStart(2, '0') + '</span>' +
                 '  <span class="batch-prog-seg-name">' + escapeHtml(s.name) + '</span>' +
                 '  <span class="batch-prog-seg-bodies">' + s.email_count + ' bodies</span>' +
                 '</div>';
      });
      segWrap.innerHTML = html2;
    }
  }

  function formatBatchDuration(sec) {
    if (sec < 60) return Math.round(sec) + 's';
    var m = Math.floor(sec / 60);
    var s = Math.floor(sec - m * 60);
    return m + 'm ' + String(s).padStart(2, '0') + 's';
  }

  async function cancelBatchRun() {
    if (!window._batchId) return;
    if (!confirm('Cancel this batch? In-flight bodies will finish, queued bodies will skip.')) {
      return;
    }
    try {
      await fetch('/api/spintax/batch/' + encodeURIComponent(window._batchId) + '/cancel', {
        method: 'POST'
      });
      showToast('Cancel requested. Waiting for in-flight bodies to finish...', 'info');
    } catch (err) {
      showToast('Network error: ' + err.message, 'warn');
    }
  }
  window.cancelBatchRun = cancelBatchRun;

  // -----------------------------------------------------------------
  // Mode toggle (raw vs preview)
  // -----------------------------------------------------------------
  function setMode(mode) {
    window._mode = mode;
    var rawContainer = document.getElementById('raw-container');
    var previewContainer = document.getElementById('preview-container');
    var picker = document.getElementById('variant-picker');
    var rawBtn = document.getElementById('mode-raw');

    if (rawBtn) {
      rawBtn.classList.toggle('seg-btn--active', mode === 'raw');
      rawBtn.setAttribute('aria-pressed', mode === 'raw' ? 'true' : 'false');
    }

    if (mode === 'raw') {
      if (rawContainer) rawContainer.style.display = '';
      if (previewContainer) previewContainer.style.display = 'none';
      if (picker) picker.value = '';
      // Close QA panel if open
      var qaSlot = document.getElementById('qa-panel-slot');
      if (qaSlot) qaSlot.innerHTML = '';
      var qaBtn = document.querySelector('.badge-btn[aria-expanded="true"]');
      if (qaBtn) { qaBtn.setAttribute('aria-expanded', 'false'); qaBtn.innerHTML = 'QA issues &darr;'; }
    }
    // preview mode is handled exclusively by setVariant()
  }
  window.setMode = setMode;

  // -----------------------------------------------------------------
  // Copy + download
  // -----------------------------------------------------------------
  async function copyRaw() {
    var text = window._rawSpintax;
    if (!text) return;
    var btn = document.getElementById('copy-btn');
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
      } else {
        fallbackCopy(text);
      }
      if (btn) {
        btn.textContent = 'Copied!';
        btn.classList.add('btn-ghost--success');
        setTimeout(function () {
          btn.textContent = 'Copy spintax';
          btn.classList.remove('btn-ghost--success');
        }, 2000);
      }
    } catch (e) {
      fallbackCopy(text);
    }
  }
  window.copyRaw = copyRaw;

  function fallbackCopy(text) {
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); } catch (e) { /* swallow */ }
    document.body.removeChild(ta);
  }

  function downloadRaw() {
    var text = window._rawSpintax;
    if (!text) return;
    var ts = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
    var filename = 'spintax_' + ts + '.txt';
    var blob = new Blob([text], { type: 'text/plain' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }
  window.downloadRaw = downloadRaw;

  // -----------------------------------------------------------------
  // Toast
  // -----------------------------------------------------------------
  function showToast(message, type) {
    type = type || 'info';
    var slot = document.getElementById('toast-slot');
    if (!slot) return;
    var toast = document.createElement('div');
    toast.className = 'toast';
    toast.setAttribute('role', 'alert');
    toast.setAttribute('aria-live', 'polite');
    toast.dataset.type = type;
    toast.innerHTML =
      '<span class="toast-icon">' + ICON_WARN + '</span>' +
      '<span class="toast-message">' + escapeHtml(message) + '</span>' +
      '<button class="toast-close" aria-label="Dismiss" type="button">x</button>';
    var closeBtn = toast.querySelector('.toast-close');
    if (closeBtn) {
      closeBtn.addEventListener('click', function () { toast.remove(); });
    }
    slot.appendChild(toast);
    setTimeout(function () {
      if (toast.parentElement) toast.remove();
    }, 5000);
  }

  // -----------------------------------------------------------------
  // Cap banner
  // -----------------------------------------------------------------
  function showCapBanner(details) {
    var banner = document.getElementById('cap-banner');
    var resetEl = document.getElementById('cap-reset-time');
    if (!banner) return;
    if (resetEl && details && details.resets_at) {
      try {
        var resetTime = new Date(details.resets_at);
        var now = new Date();
        var diffMs = resetTime - now;
        if (diffMs > 0) {
          var hours = Math.floor(diffMs / (1000 * 60 * 60));
          var mins = Math.floor((diffMs / (1000 * 60)) % 60);
          resetEl.textContent = hours + 'h ' + mins + 'm';
        } else {
          resetEl.textContent = 'soon';
        }
      } catch (e) {
        resetEl.textContent = 'soon';
      }
    }
    banner.style.display = '';
  }

  // -----------------------------------------------------------------
  // Generate / poll
  // -----------------------------------------------------------------
  async function startGeneration() {
    var ta = document.getElementById('email-body');
    var body = ta ? ta.value.trim() : '';
    if (!body) {
      showInputError();
      return;
    }

    // Mode dispatch: if the input looks like a multi-segment .md doc,
    // route into the batch flow (parse -> confirm -> spin -> zip).
    // Otherwise stay on the existing single-email path.
    if (detectIsBatch(body)) {
      return startBatchParse();
    }

    hideInputError();
    clearErrorCard();
    setState('queued');
    setGenerateButtonLoading(true);
    window._startTime = Date.now();
    startElapsedTimer();

    try {
      var resp = await fetch('/api/spintax', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: body, platform: window._platform, model: window._model || 'o3' })
      });

      if (resp.status === 429) {
        var data429 = {};
        try { data429 = await resp.json(); } catch (e) { /* swallow */ }
        showCapBanner(data429.details || {});
        setState('idle');
        setGenerateButtonLoading(false);
        return;
      }

      if (resp.status === 401) {
        showToast('Session expired. Redirecting to login...', 'warn');
        setTimeout(function () { window.location.href = '/login'; }, 3000);
        return;
      }

      if (!resp.ok) {
        setState('failed', { error: 'submit_failed' });
        setGenerateButtonLoading(false);
        return;
      }

      var json = await resp.json();
      window._jobId = json.job_id;
      window._pollInterval = setInterval(pollStatus, POLL_INTERVAL_MS);
      // Trigger an immediate first poll so the UI advances quickly.
      pollStatus();
    } catch (err) {
      setState('failed', { error: 'network_error' });
      setGenerateButtonLoading(false);
    }
  }
  window.startGeneration = startGeneration;

  async function pollStatus() {
    if (!window._jobId) return;
    try {
      var resp = await fetch('/api/status/' + encodeURIComponent(window._jobId));

      if (resp.status === 401) {
        stopPolling();
        showToast('Session expired. Redirecting to login...', 'warn');
        setTimeout(function () { window.location.href = '/login'; }, 3000);
        return;
      }

      if (resp.status === 404) {
        stopPolling();
        setState('failed', { error: 'job_not_found' });
        setGenerateButtonLoading(false);
        return;
      }

      if (!resp.ok) {
        // Transient - skip this tick
        return;
      }

      var data = await resp.json();
      updateElapsedTime();

      if (TERMINAL_STATES.indexOf(data.status) !== -1) {
        stopPolling();
        setGenerateButtonLoading(false);
      }
      setState(data.status, data);
    } catch (err) {
      // Network blip - keep polling
    }
  }

  function stopPolling() {
    if (window._pollInterval) {
      clearInterval(window._pollInterval);
      window._pollInterval = null;
    }
  }

  function retryGeneration() {
    clearErrorCard();
    setState('idle');
    setGenerateButtonLoading(false);
    window._jobId = null;
    window._rawSpintax = null;
    var output = document.getElementById('output');
    if (output) output.style.display = 'none';
    startGeneration();
  }
  window.retryGeneration = retryGeneration;

  // -----------------------------------------------------------------
  // Tool switcher
  // -----------------------------------------------------------------
  function toggleToolSwitcher(btn) {
    var menu = btn.nextElementSibling;
    if (!menu) return;
    var isOpen = menu.classList.toggle('is-open');
    btn.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
  }
  window.toggleToolSwitcher = toggleToolSwitcher;

  // -----------------------------------------------------------------
  // Init
  // -----------------------------------------------------------------
  document.addEventListener('DOMContentLoaded', function () {
    var ta = document.getElementById('email-body');
    if (ta) {
      ta.addEventListener('input', function () {
        updateInputWordCount();
        updateGenerateButtonEnabled();
        updateModeIndicator();
        if (ta.value.trim()) hideInputError();
      });
      ta.addEventListener('keydown', function (e) {
        if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
          startGeneration();
        }
      });
    }
    updateInputWordCount();
    // BUG-02: disable Generate button on initial load when textarea is empty.
    updateGenerateButtonEnabled();
    updateModeIndicator();
    setState('idle');

    // Close tool switcher on outside click
    document.addEventListener('click', function (e) {
      if (!e.target.closest('.tool-switcher')) {
        document.querySelectorAll('.tool-switcher-menu.is-open').forEach(function (m) {
          m.classList.remove('is-open');
          var ts = m.parentElement && m.parentElement.querySelector('.tool-switcher-btn');
          if (ts) ts.setAttribute('aria-expanded', 'false');
        });
      }
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') {
        document.querySelectorAll('.tool-switcher-menu.is-open').forEach(function (m) {
          m.classList.remove('is-open');
          var ts = m.parentElement && m.parentElement.querySelector('.tool-switcher-btn');
          if (ts) ts.setAttribute('aria-expanded', 'false');
        });
      }
    });
  });
})();
