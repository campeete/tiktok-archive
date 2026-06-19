// tiktok-archive frontend JS
// Vanilla, no framework. Each section guarded so pages without the hooks don't crash.

(() => {
  'use strict';

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const escapeHTML = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));

  function show(el)    { if (el) el.classList.remove('hidden'); }
  function hide(el)    { if (el) el.classList.add('hidden'); }
  function setStatus(el, msg, kind = '') {
    if (!el) return;
    el.classList.remove('hidden', 'error');
    if (kind) el.classList.add(kind);
    el.textContent = msg;
  }

  // ---------- analyze form ----------
  function wireAnalyze() {
    const form = $('#analyze-form');
    if (!form) return;

    const urlInput  = $('#analyze-url');
    const fileInput = $('#analyze-file');
    const fileLabel = $('#file-label');
    const submit    = $('#analyze-submit');
    const resetBtn  = $('#analyze-reset');
    const progress  = $('#analyze-progress');
    const status    = $('#analyze-status');
    const result    = $('#analyze-result');
    const resultBody = $('#analyze-result-body');
    const asideStatus  = $('#aside-status-text');
    const asideProgress = $('#aside-progress');

    fileInput?.addEventListener('change', () => {
      const f = fileInput.files?.[0];
      fileLabel.textContent = f ? f.name : '';
      if (f) urlInput.value = '';
    });
    urlInput?.addEventListener('input', () => {
      if (urlInput.value && fileInput.value) {
        fileInput.value = '';
        fileLabel.textContent = '';
      }
    });

    resetBtn?.addEventListener('click', () => {
      hide(result);
      hide(progress);
      hide(status);
      fileLabel.textContent = '';
      if (asideStatus) asideStatus.textContent = 'Idle. Submit a video to begin.';
      asideProgress?.classList.add('idle');
    });

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      hide(result);
      const url = urlInput.value.trim();
      const file = fileInput.files?.[0];
      if (!url && !file) {
        setStatus(status, 'Paste a URL or pick a file first.', 'error');
        show(status);
        return;
      }
      submit.disabled = true;
      show(progress);
      setStatus(status, 'Analyzing... this can take 30-90 seconds for a fresh video.');
      show(status);
      if (asideStatus) asideStatus.textContent = 'Working...';
      asideProgress?.classList.remove('idle');

      const stages = [
        'Downloading...',
        'Extracting audio...',
        'Transcribing (Whisper)...',
        'Tagging (Ollama)...',
        'Finalizing...',
      ];
      let stageIdx = 0;
      const fakeTimer = setInterval(() => {
        if (stageIdx < stages.length) {
          setStatus(status, stages[stageIdx]);
          if (asideStatus) asideStatus.textContent = stages[stageIdx];
          stageIdx++;
        }
      }, 8000);

      try {
        const fd = new FormData();
        if (url) fd.append('url', url);
        if (file) fd.append('file', file);

        const resp = await fetch('/api/analyze', { method: 'POST', body: fd });
        const data = await resp.json();
        clearInterval(fakeTimer);
        hide(progress);

        if (!data.ok) {
          setStatus(status,
            `Failed at stage "${data.stage || '?'}": ${data.error || 'unknown error'}`,
            'error');
          if (asideStatus) asideStatus.textContent = 'Failed.';
          asideProgress?.classList.add('idle');
          return;
        }
        setStatus(status, `Done in ${data.elapsed_sec?.toFixed(1)}s.`);
        if (asideStatus) asideStatus.textContent = 'Done. Review result below.';
        asideProgress?.classList.add('idle');
        renderResult(resultBody, data);
        show(result);
      } catch (err) {
        clearInterval(fakeTimer);
        hide(progress);
        setStatus(status, `Network error: ${err.message}`, 'error');
        if (asideStatus) asideStatus.textContent = 'Network error.';
        asideProgress?.classList.add('idle');
      } finally {
        submit.disabled = false;
      }
    });
  }

  function renderResult(el, data) {
    const topics = (data.topics || []).map(t =>
      `<span class="tag">${escapeHTML(t)}</span>`).join(' ');
    const points = (data.key_points || []).map(p =>
      `<li>${escapeHTML(p)}</li>`).join('');
    el.innerHTML = `
      <p><strong>Summary:</strong> ${escapeHTML(data.summary || '(none)')}</p>
      ${points ? `<p><strong>Key Points:</strong></p><ul class="bullets">${points}</ul>` : ''}
      <p><strong>Tags:</strong></p>
      <div class="tag-row">
        ${data.intent ? `<span class="tag">intent: ${escapeHTML(data.intent)}</span>` : ''}
        ${topics}
        ${data.claim_check ? '<span class="tag tag-warn">claim-check</span>' : ''}
      </div>
      <p style="margin-top:16px"><a class="button-link" href="/v/${data.video_id}">View Full Detail</a></p>
    `;
  }

  // ---------- Q&A ----------
  function wireQA() {
    const form = $('#qa-form');
    if (!form) return;
    const input    = $('#qa-input');
    const thread   = $('#qa-thread');
    const progress = $('#qa-progress');
    const clearBtn = $('#qa-clear');
    const videoId  = form.dataset.videoId;

    clearBtn?.addEventListener('click', () => { thread.innerHTML = ''; });

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const q = input.value.trim();
      if (!q) return;
      input.value = '';

      const item = document.createElement('div');
      item.className = 'qa-item';
      item.innerHTML = `
        <div class="qa-q">${escapeHTML(q)}</div>
        <div class="qa-a qa-pending">thinking...</div>
      `;
      thread.appendChild(item);
      show(progress);

      try {
        const resp = await fetch(`/api/ask/${videoId}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ question: q }),
        });
        const data = await resp.json();
        const ansEl = item.querySelector('.qa-a');
        ansEl.classList.remove('qa-pending');
        ansEl.textContent = data.ok ? data.answer : `error: ${data.error}`;
      } catch (err) {
        const ansEl = item.querySelector('.qa-a');
        ansEl.classList.remove('qa-pending');
        ansEl.textContent = `network error: ${err.message}`;
      } finally {
        hide(progress);
      }
    });
  }

  // ---------- creators page ----------
  function wireCreatorSync() {
    $$('.sync-btn').forEach(btn => {
      btn.addEventListener('click', async (e) => {
        e.preventDefault();
        const handle = btn.dataset.handle;
        const original = btn.textContent;
        btn.textContent = 'Queueing...';
        btn.disabled = true;
        try {
          const resp = await fetch(`/api/sync/${encodeURIComponent(handle)}`, { method: 'POST' });
          const data = await resp.json();
          btn.textContent = data.ok ? 'Queued' : 'Error';
          await sleep(1500);
        } catch (err) {
          btn.textContent = 'Error';
          await sleep(1500);
        } finally {
          btn.textContent = original;
          btn.disabled = false;
        }
      });
    });

    const addForm = $('#add-creator-form');
    if (!addForm) return;
    const handleInput = $('#add-creator-handle');
    const status      = $('#add-creator-status');
    const progress    = $('#add-creator-progress');

    addForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const handle = handleInput.value.trim().replace(/^@/, '');
      if (!handle) {
        setStatus(status, 'Enter a handle.', 'error');
        show(status);
        return;
      }
      show(progress);
      setStatus(status, `Adding @${handle} and queueing sync...`);
      show(status);
      try {
        const resp = await fetch(`/api/sync/${encodeURIComponent(handle)}`, { method: 'POST' });
        const data = await resp.json();
        hide(progress);
        if (data.ok) {
          setStatus(status, `@${data.handle} added and sync queued. Start a worker if one is not running.`);
          await sleep(900);
          window.location.reload();
        } else {
          setStatus(status, `Error: ${data.error}`, 'error');
        }
      } catch (err) {
        hide(progress);
        setStatus(status, `Network error: ${err.message}`, 'error');
      }
    });
  }

  // ---------- queue page polling ----------
  let queuePollHandle = null;

  async function refreshQueue() {
    try {
      const resp = await fetch('/api/queue');
      const data = await resp.json();
      renderQueueSummary(data.stats || {});
      renderQueueRows(data.recent || []);
    } catch (err) {
      const tbody = $('#queue-rows');
      if (tbody) tbody.innerHTML = `<tr><td colspan="8">Failed to fetch: ${escapeHTML(err.message)}</td></tr>`;
    }
  }

  function renderQueueSummary(stats) {
    const status = stats.by_status || {};
    const setVal = (id, v) => {
      const el = document.getElementById(id);
      if (el) el.textContent = v;
    };
    setVal('stat-pending', status.pending || 0);
    setVal('stat-running', status.running || 0);
    setVal('stat-done',    status.done    || 0);
    setVal('stat-failed',  status.failed  || 0);
    setVal('stat-recent-failures', stats.recent_failures_24h || 0);
  }

  function renderQueueRows(recent) {
    const tbody = $('#queue-rows');
    if (!tbody) return;
    if (recent.length === 0) {
      tbody.innerHTML = `<tr><td colspan="8">Queue is empty.</td></tr>`;
      return;
    }
    tbody.innerHTML = recent.map(j => {
      const dur = j.duration_sec != null ? `${j.duration_sec.toFixed(1)}s` : '';
      const note = (j.last_error || '').slice(0, 80);
      const videoLink = j.video_id ? `<a href="/v/${j.video_id}">#${j.video_id}</a>` : '';
      return `
        <tr>
          <td>${j.id}</td>
          <td>${escapeHTML(j.kind)}</td>
          <td>${escapeHTML(j.status)}</td>
          <td>${videoLink}</td>
          <td>${j.creator_id ?? ''}</td>
          <td>${j.attempts}</td>
          <td>${dur}</td>
          <td>${escapeHTML(note)}</td>
        </tr>
      `;
    }).join('');
  }

  function startQueuePoll() {
    if (queuePollHandle != null) return;
    refreshQueue();
    queuePollHandle = setInterval(refreshQueue, 5000);
  }
  window.startQueuePoll = startQueuePoll;

  // ---------- importance toggle (Phase 1.7) ----------
  function wireImportanceToggle() {
    const btn = document.getElementById('imp-toggle');
    if (!btn) return;
    btn.addEventListener('click', async () => {
      const videoId = btn.dataset.videoId;
      const current = btn.dataset.current === 'true';
      const target = !current;
      btn.disabled = true;
      const originalText = btn.textContent;
      btn.textContent = '...';
      try {
        const r = await fetch(`/api/importance/${videoId}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ important: target }),
        });
        const data = await r.json();
        if (!data.ok) throw new Error(data.error || 'failed');
        // Reload — easier than mutating the DOM piecemeal, and shows
        // the user the new state (deleted full-res, updated reason).
        window.location.reload();
      } catch (e) {
        alert('Could not update importance: ' + e.message);
        btn.disabled = false;
        btn.textContent = originalText;
      }
    });
  }

  // ---------- init ----------
  document.addEventListener('DOMContentLoaded', () => {
    wireAnalyze();
    wireQA();
    wireCreatorSync();
    wireImportanceToggle();
  });
})();
