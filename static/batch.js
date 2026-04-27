/* Prospeqt Spintax Batch — batch.js
 *
 * Drives /batch end-to-end:
 *   input -> parsing -> confirm -> progress -> done
 *
 * State machine (single global var window._batchStage):
 *   'input'    — user pasting/uploading the .md
 *   'parsing'  — POST /api/spintax/batch with dry_run=true in flight
 *   'confirm'  — parser returned; user reviews segment list
 *   'progress' — POST /api/spintax/batch dry_run=false fired; polling
 *   'done'     — terminal; download .zip button visible
 *   'failed'   — parser/runner hard-fail
 *   'cancelled'— user clicked cancel mid-batch
 */

(function () {
  'use strict';

  // ---------------------------------------------------------------
  // State
  // ---------------------------------------------------------------
  window._batchStage = 'input';
  window._batchPlatform = 'instantly';
  window._batchModel = 'o3';
  window._batchId = null;
  window._batchPollInterval = null;
  window._batchParsed = null;       // BatchParsedSummary block

  var POLL_MS = 5000;

  // ---------------------------------------------------------------
  // Utilities
  // ---------------------------------------------------------------
  function $(id) { return document.getElementById(id); }

  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function setStage(stage) {
    window._batchStage = stage;
    document.body.dataset.state = stage;
    var sections = ['batch-input', 'batch-parsing', 'batch-confirm', 'batch-progress'];
    sections.forEach(function (id) {
      var el = $(id);
      if (!el) return;
      el.style.display = (id === 'batch-' + stage || (stage === 'done' && id === 'batch-progress'))
        ? ''
        : 'none';
    });
  }

  // ---------------------------------------------------------------
  // Input stage
  // ---------------------------------------------------------------
  function updateCharCount() {
    var ta = $('md-input');
    var cc = $('md-char-count');
    if (!ta || !cc) return;
    var n = ta.value.length;
    cc.textContent = n.toLocaleString() + ' chars';
  }

  function updateParseButton() {
    var ta = $('md-input');
    var btn = $('parse-btn');
    if (!ta || !btn) return;
    btn.disabled = !ta.value.trim();
  }

  function setBatchPlatform(p) {
    window._batchPlatform = p;
    var btns = document.querySelectorAll('[data-platform]');
    btns.forEach(function (b) {
      var active = b.dataset.platform === p;
      b.classList.toggle('seg-btn--active', active);
      b.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
  }
  window.setBatchPlatform = setBatchPlatform;

  function setBatchModel(m) {
    window._batchModel = m;
  }
  window.setBatchModel = setBatchModel;

  function handleFileSelect(event) {
    var f = event.target.files && event.target.files[0];
    if (!f) return;
    $('batch-file-name').textContent = f.name;
    var reader = new FileReader();
    reader.onload = function () {
      $('md-input').value = String(reader.result || '');
      updateCharCount();
      updateParseButton();
    };
    reader.onerror = function () {
      showToast('Could not read file: ' + f.name, 'warn');
    };
    reader.readAsText(f);
  }
  window.handleFileSelect = handleFileSelect;

  function showInputError(msg) {
    var err = $('batch-input-error');
    if (!err) return;
    if (msg) err.textContent = msg;
    err.style.display = '';
  }

  function hideInputError() {
    var err = $('batch-input-error');
    if (err) err.style.display = 'none';
  }

  function resetToInput() {
    if (window._batchPollInterval) {
      clearInterval(window._batchPollInterval);
      window._batchPollInterval = null;
    }
    window._batchId = null;
    window._batchParsed = null;
    setStage('input');
  }
  window.resetToInput = resetToInput;

  // ---------------------------------------------------------------
  // Parse (dry run)
  // ---------------------------------------------------------------
  async function startParse() {
    var md = $('md-input').value.trim();
    if (!md) {
      showInputError('Paste markdown content or upload a .md file before parsing.');
      return;
    }
    hideInputError();
    setStage('parsing');

    try {
      var resp = await fetch('/api/spintax/batch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          md: md,
          platform: window._batchPlatform,
          model: window._batchModel,
          concurrency: parseInt($('batch-concurrency').value, 10) || 5,
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
        showInputError(String(msg));
        setStage('input');
        return;
      }

      window._batchId = data.batch_id;
      window._batchParsed = data.parsed;
      renderConfirmPanel(data);
      setStage('confirm');
    } catch (err) {
      showInputError('Network error: ' + err.message);
      setStage('input');
    }
  }
  window.startParse = startParse;

  function renderConfirmPanel(data) {
    var p = data.parsed;
    var stat = $('batch-confirm-stat');
    if (stat) {
      stat.textContent = p.segments.length + ' segments · ' + p.total_bodies + ' bodies';
    }
    $('confirm-count').textContent = p.total_bodies;

    var warnEl = $('batch-warnings');
    if (warnEl) {
      if (p.warnings && p.warnings.length) {
        warnEl.innerHTML = '<strong>Warnings:</strong><ul>' +
          p.warnings.map(function (w) { return '<li>' + escapeHtml(w) + '</li>'; }).join('') +
          '</ul>';
        warnEl.style.display = '';
      } else {
        warnEl.style.display = 'none';
      }
    }

    // Group by section
    var bySection = {};
    var orderedSections = [];
    p.segments.forEach(function (s) {
      var sec = s.section || '(no section)';
      if (!(sec in bySection)) {
        bySection[sec] = [];
        orderedSections.push(sec);
      }
      bySection[sec].push(s);
    });

    var html = '';
    orderedSections.forEach(function (sec) {
      var segs = bySection[sec];
      var bodies = segs.reduce(function (a, s) { return a + s.email_count; }, 0);
      html += '<div class="batch-section-group">';
      html += '  <div class="batch-section-header">' + escapeHtml(sec) +
              ' <span class="batch-section-meta">(' + segs.length + ' seg · ' + bodies + ' bodies)</span></div>';
      html += '  <ol class="batch-segment-rows">';
      segs.forEach(function (s) {
        var warns = (s.warnings && s.warnings.length)
          ? '<span class="batch-seg-warn">⚠ ' + escapeHtml(s.warnings.join(', ')) + '</span>'
          : '';
        html += '    <li class="batch-seg-row">' +
                '      <span class="batch-seg-name">' + escapeHtml(s.name) + '</span>' +
                '      <span class="batch-seg-emails">' + s.email_count + ' ✉</span>' +
                warns +
                '    </li>';
      });
      html += '  </ol>';
      html += '</div>';
    });
    $('batch-segment-list').innerHTML = html;

    // Crude cost estimate text — server returns a number on real submit
    var est = $('batch-cost-estimate');
    if (est) {
      est.textContent = 'Estimated cost will be shown once the batch starts. ' +
        'Subjects pass through verbatim - only the ' + p.total_bodies + ' bodies get spintaxed.';
    }
  }

  // ---------------------------------------------------------------
  // Spin all (real run)
  // ---------------------------------------------------------------
  async function startBatch() {
    if (!window._batchId) return;
    var md = $('md-input').value.trim();
    if (!md) return;

    // IMMEDIATE feedback — disable button, show progress panel with a
    // submitting state, BEFORE the fetch so the user sees the click
    // landed. Submit POST takes 1-15s depending on parser load.
    var btn = $('confirm-btn');
    if (btn) {
      btn.disabled = true;
      btn.dataset.origLabel = btn.innerHTML;
      btn.innerHTML = '<span class="spinner-white"></span>Submitting...';
    }
    showSubmittingProgress();
    setStage('progress');

    // Re-submit without dry_run. The server creates a fresh batch_id —
    // the dry-run batch_id is informational only.
    try {
      var resp = await fetch('/api/spintax/batch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          md: md,
          platform: window._batchPlatform,
          model: window._batchModel,
          concurrency: parseInt($('batch-concurrency').value, 10) || 5,
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
        // Roll back to confirm so user can retry.
        if (btn) {
          btn.disabled = false;
          if (btn.dataset.origLabel) btn.innerHTML = btn.dataset.origLabel;
        }
        setStage('confirm');
        return;
      }

      window._batchId = data.batch_id;
      window._batchParsed = data.parsed;
      // Initial render with parsed data so the rows show up before the
      // first poll returns.
      renderProgress({
        batch_id: data.batch_id,
        status: 'running',
        platform: window._batchPlatform,
        model: window._batchModel,
        completed: 0,
        failed: 0,
        in_progress: 0,
        retrying: 0,
        queued: data.total_jobs,
        total: data.total_jobs,
        retries_used: 0,
        elapsed_sec: 0,
        cost_usd_so_far: 0,
        cost_usd_estimated_total: 0,
        failure_reason: null,
        download_url: null,
        parsed: data.parsed
      });
      startPolling();
    } catch (err) {
      showToast('Network error: ' + err.message, 'warn');
      if (btn) {
        btn.disabled = false;
        if (btn.dataset.origLabel) btn.innerHTML = btn.dataset.origLabel;
      }
      setStage('confirm');
    }
  }
  window.startBatch = startBatch;

  function showSubmittingProgress() {
    var titleEl = $('batch-progress-title');
    var metaEl = $('batch-progress-meta');
    var statsEl = $('batch-progress-stats');
    var actionsEl = $('batch-progress-actions');
    var bar = $('batch-progress-bar');
    if (titleEl) titleEl.textContent = 'Submitting batch...';
    if (metaEl) metaEl.textContent = 'Firing OpenAI jobs in 5 parallel workers...';
    if (statsEl) statsEl.innerHTML = '<span class="batch-stat">Submitting...</span>';
    if (actionsEl) actionsEl.innerHTML = '';
    if (bar) {
      bar.style.width = '5%';
      bar.dataset.status = 'running';
    }
  }

  function startPolling() {
    if (window._batchPollInterval) clearInterval(window._batchPollInterval);
    window._batchPollInterval = setInterval(pollBatch, POLL_MS);
    pollBatch();
  }

  function stopPolling() {
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
        stopPolling();
        showToast('Session expired. Redirecting...', 'warn');
        setTimeout(function () { window.location.href = '/login'; }, 2000);
        return;
      }
      if (resp.status === 404) {
        stopPolling();
        showToast('Batch not found (may have expired)', 'warn');
        return;
      }
      if (!resp.ok) return;  // transient
      var data = await resp.json();
      renderProgress(data);
      if (data.status === 'done' || data.status === 'failed' || data.status === 'cancelled') {
        stopPolling();
      }
    } catch (err) {
      // network blip — keep polling
    }
  }

  // ---------------------------------------------------------------
  // Progress render
  // ---------------------------------------------------------------
  function renderProgress(data) {
    var titleEl = $('batch-progress-title');
    var metaEl = $('batch-progress-meta');
    var actionsEl = $('batch-progress-actions');

    var done = data.completed + data.failed;
    var pct = data.total > 0 ? Math.round((done / data.total) * 100) : 0;

    var statusText;
    if (data.status === 'done') {
      statusText = 'Done';
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
      parts.push(formatDuration(data.elapsed_sec) + ' elapsed');
      parts.push('$' + data.cost_usd_so_far.toFixed(2) + ' spent');
      if (data.cost_usd_estimated_total) {
        parts.push('~$' + data.cost_usd_estimated_total.toFixed(2) + ' estimated total');
      }
      if (data.retries_used > 0) {
        parts.push(data.retries_used + ' retries');
      }
      metaEl.textContent = parts.join(' · ');
    }

    var bar = $('batch-progress-bar');
    if (bar) {
      bar.style.width = pct + '%';
      bar.dataset.status = data.status;
    }

    var stats = $('batch-progress-stats');
    if (stats) {
      stats.innerHTML =
        '<span class="batch-stat batch-stat--done"><strong>' + data.completed + '</strong> done</span>' +
        '<span class="batch-stat batch-stat--running"><strong>' + data.in_progress + '</strong> running</span>' +
        '<span class="batch-stat batch-stat--retry"><strong>' + data.retrying + '</strong> retrying</span>' +
        '<span class="batch-stat batch-stat--queued"><strong>' + data.queued + '</strong> queued</span>' +
        '<span class="batch-stat batch-stat--failed"><strong>' + data.failed + '</strong> failed</span>';
    }

    // Actions: Cancel while running, Download when terminal.
    if (actionsEl) {
      var html = '';
      if (data.status === 'running') {
        html += '<button class="btn btn-ghost" id="batch-cancel-btn" onclick="cancelBatch()">Cancel batch</button>';
      } else {
        html += '<button class="btn btn-ghost" onclick="resetToInput()">New batch</button>';
        if (data.download_url) {
          html += '<a class="btn btn--full" href="' + escapeHtml(data.download_url) + '" download>Download .zip</a>';
        }
      }
      actionsEl.innerHTML = html;
    }

    // Per-segment progress (best-effort: we only have aggregate counts,
    // so for v1 we just list the segments + a body count by section).
    var segWrap = $('batch-segment-progress');
    if (segWrap && data.parsed) {
      var html = '';
      data.parsed.segments.forEach(function (s, i) {
        html += '<div class="batch-prog-seg">' +
                '  <span class="batch-prog-seg-num">' + String(i + 1).padStart(2, '0') + '</span>' +
                '  <span class="batch-prog-seg-name">' + escapeHtml(s.name) + '</span>' +
                '  <span class="batch-prog-seg-bodies">' + s.email_count + ' bodies</span>' +
                '</div>';
      });
      segWrap.innerHTML = html;
    }
  }

  function formatDuration(sec) {
    if (sec < 60) return Math.round(sec) + 's';
    var m = Math.floor(sec / 60);
    var s = Math.floor(sec - m * 60);
    return m + 'm ' + String(s).padStart(2, '0') + 's';
  }

  // ---------------------------------------------------------------
  // Cancel
  // ---------------------------------------------------------------
  async function cancelBatch() {
    if (!window._batchId) return;
    if (!confirm('Cancel this batch? In-flight bodies will finish, queued bodies will skip.')) {
      return;
    }
    try {
      await fetch('/api/spintax/batch/' + encodeURIComponent(window._batchId) + '/cancel', {
        method: 'POST'
      });
      // Status update will arrive via the next poll.
      showToast('Cancel requested. Waiting for in-flight bodies to finish...', 'info');
    } catch (err) {
      showToast('Network error: ' + err.message, 'warn');
    }
  }
  window.cancelBatch = cancelBatch;

  // ---------------------------------------------------------------
  // Toast (lightweight; mirrors main.js shape)
  // ---------------------------------------------------------------
  function showToast(message, type) {
    type = type || 'info';
    var slot = $('toast-slot');
    if (!slot) return;
    var toast = document.createElement('div');
    toast.className = 'toast';
    toast.dataset.type = type;
    toast.setAttribute('role', 'alert');
    toast.innerHTML = '<span class="toast-message">' + escapeHtml(message) +
      '</span><button class="toast-close" aria-label="Dismiss">x</button>';
    var close = toast.querySelector('.toast-close');
    if (close) close.addEventListener('click', function () { toast.remove(); });
    slot.appendChild(toast);
    setTimeout(function () { if (toast.parentElement) toast.remove(); }, 8000);
  }

  // ---------------------------------------------------------------
  // Init
  // ---------------------------------------------------------------
  document.addEventListener('DOMContentLoaded', function () {
    var ta = $('md-input');
    if (ta) {
      ta.addEventListener('input', function () {
        updateCharCount();
        updateParseButton();
      });
    }
    updateCharCount();
    updateParseButton();
    setStage('input');
  });
})();
