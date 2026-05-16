// ── Ingest result rendering ───────────────────────────────────────────────────
function renderIngestResults(results, target) {
  var hasSuccess = false;
  var html = '';
  for (var r of results) {
    if (r.error) {
      html += '<div class="ingest-error"><strong>' + (r.source || 'File') + '</strong>: ' + r.error + '</div>';
    } else {
      hasSuccess = true;
      var alpha  = r.alphabetical ? '<span class="tag">alphabetical</span>' : '';
      var action = r.reimport ? 're-imported' : 'ingested';
      var detail = r.reimport
        ? r.bands_added + ' new &middot; ' + r.bands_merged + ' existing (scores &amp; notes kept)'
        : r.bands_added + ' new &middot; ' + r.bands_merged + ' merged';
      html += `
        <div class="ingest-result-card">
          <div class="ingest-success-icon">&#10003;</div>
          <strong>${r.festival_name || r.source || 'Festival'}</strong> ${action}
          &mdash; <span class="tag">${r.band_count} bands</span> ${alpha}
          <br><span class="muted">${detail}</span>
        </div>`;
    }
  }
  if (hasSuccess) {
    html += '<p class="muted" style="font-size:0.8rem;margin-top:0.5rem">Refreshing dashboard in 3 seconds&hellip;</p>';
    setTimeout(() => location.reload(), 3000);
  }
  target.innerHTML = html || '<div class="ingest-error">No results returned.</div>';
}

// ── Background job tracking (persists across navigation via localStorage) ─────
var _ftPolling = {};  // jobId → interval handle; prevents duplicate pollers

function ftGetJobs() {
  try { return JSON.parse(localStorage.getItem('ftJobs') || '{}'); } catch (e) { return {}; }
}
function ftSaveJobs(jobs) {
  try { localStorage.setItem('ftJobs', JSON.stringify(jobs)); } catch (e) {}
}

function ftTrackJob(jobId, displays, resultEl) {
  var jobs = ftGetJobs();
  jobs[jobId] = { id: jobId, displays: displays, status: 'queued', startedAt: Date.now(), results: [] };
  ftSaveJobs(jobs);
  ftUpdateHeaderIndicator();
  ftPollJob(jobId, resultEl);
}

function ftPollJob(jobId, resultEl) {
  if (_ftPolling[jobId]) return;
  _ftPolling[jobId] = setInterval(async function () {
    try {
      var resp = await fetch('/ingest/jobs/' + jobId);
      if (resp.status === 404) {
        // Server restarted — job is gone
        clearInterval(_ftPolling[jobId]);
        delete _ftPolling[jobId];
        var jobs = ftGetJobs();
        if (jobs[jobId]) {
          jobs[jobId].status = 'done';
          jobs[jobId].results = [{ source: jobs[jobId].displays[0] || 'poster', error: 'Server restarted — please re-upload' }];
          jobs[jobId].completedAt = Date.now();
          ftSaveJobs(jobs);
        }
        ftUpdateHeaderIndicator();
        return;
      }
      var data = await resp.json();
      if (data.status === 'done') {
        clearInterval(_ftPolling[jobId]);
        delete _ftPolling[jobId];
        var jobs = ftGetJobs();
        if (jobs[jobId]) {
          jobs[jobId].status = 'done';
          jobs[jobId].results = data.results;
          jobs[jobId].completedAt = Date.now();
          ftSaveJobs(jobs);
        }
        ftUpdateHeaderIndicator();
        // If the result element is still on this page, render inline
        if (resultEl && document.body.contains(resultEl)) {
          renderIngestResults(data.results, resultEl);
        }
      }
    } catch (e) {}
  }, 3000);
}

function ftUpdateHeaderIndicator() {
  var indicator = document.getElementById('job-indicator');
  if (!indicator) return;
  var jobs = ftGetJobs();
  var now = Date.now();

  // Prune jobs completed more than 5 minutes ago
  var pruned = false;
  for (var jid in jobs) {
    if (jobs[jid].status === 'done' && now - (jobs[jid].completedAt || 0) > 300000) {
      delete jobs[jid];
      pruned = true;
    }
    // Expire stuck jobs older than 15 minutes
    if (jobs[jid].status !== 'done' && now - (jobs[jid].startedAt || 0) > 900000) {
      jobs[jid].status = 'done';
      jobs[jid].results = [{ source: 'poster', error: 'Timed out' }];
      jobs[jid].completedAt = now;
      pruned = true;
    }
  }
  if (pruned) ftSaveJobs(jobs);

  var active = Object.values(jobs).filter(function (j) { return j.status !== 'done'; });
  var recentDone = Object.values(jobs).filter(function (j) {
    return j.status === 'done' && now - (j.completedAt || 0) < 12000;
  });

  if (active.length > 0) {
    var total = active.reduce(function (s, j) { return s + (j.displays || []).length; }, 0);
    indicator.className = 'job-indicator active';
    indicator.innerHTML = '<span class="loading-spinner"></span>&nbsp;'
      + total + ' poster' + (total !== 1 ? 's' : '') + ' processing&hellip;';
    indicator.style.display = '';
  } else if (recentDone.length > 0) {
    var donePosterCount = recentDone.reduce(function (s, j) { return s + (j.results || []).length; }, 0);
    var doneOk = recentDone.reduce(function (s, j) {
      return s + (j.results || []).filter(function (r) { return !r.error; }).length;
    }, 0);
    indicator.className = 'job-indicator done';
    indicator.innerHTML = '&#10003;&nbsp;' + doneOk + ' poster' + (doneOk !== 1 ? 's' : '') + ' ingested';
    indicator.style.display = '';
    // Auto-hide after 8 seconds
    clearTimeout(indicator._hideTimer);
    indicator._hideTimer = setTimeout(function () {
      indicator.style.display = 'none';
      indicator.className = 'job-indicator';
    }, 8000);
  } else {
    indicator.style.display = 'none';
    indicator.className = 'job-indicator';
  }
}

// On every page load: resume polling any pending jobs from localStorage
(function ftInit() {
  var jobs = ftGetJobs();
  for (var jobId in jobs) {
    if (jobs[jobId].status !== 'done') {
      ftPollJob(jobId, null);
    }
  }
  ftUpdateHeaderIndicator();
})();

// ── HTMX event handlers ───────────────────────────────────────────────────────
document.body.addEventListener('htmx:afterSwap', function (evt) {
  var target = evt.detail.target;

  // Ingest result — may now be a job receipt or a legacy full result
  if (target.id === 'ingest-result') {
    try {
      var data = JSON.parse(target.textContent);

      // New async job response
      if (data.job_id) {
        var count = data.source_count || (data.displays || []).length || 1;
        target.innerHTML = `
          <div class="ingest-progress">
            <span class="loading-spinner"></span>
            Processing ${count} poster${count !== 1 ? 's' : ''}&hellip;
            <span class="muted" style="display:block;font-size:0.8rem;margin-top:0.3rem">
              You can navigate away &mdash; processing continues in the background.
            </span>
          </div>`;
        ftTrackJob(data.job_id, data.displays || [], target);
        return;
      }

      // Legacy full-results response (fallback)
      var results = data.results || [];
      renderIngestResults(results, target);
    } catch (_) {
      target.innerHTML = '<div class="ingest-error">' + target.textContent + '</div>';
    }
  }

  // Research result
  if (target.id === 'research-status') {
    try {
      var data = JSON.parse(target.textContent);
      if (data.success) {
        target.innerHTML = '<span style="color:var(--success)">&#10003; Research complete — reload to see updates.</span>';
      } else {
        target.innerHTML = '<span style="color:var(--hot)">Research failed.</span>';
      }
    } catch (_) {}
  }

  // Rating result
  if (target.id === 'rate-status') {
    try {
      var data = JSON.parse(target.textContent);
      if (data.status === 'ok') {
        target.innerHTML = '<span style="color:var(--success)">&#10003; Rating saved.</span>';
      }
    } catch (_) {}
  }

  // Playlist result
  if (target.id === 'playlist-result') {
    try {
      var data = JSON.parse(target.textContent);
      if (data.playlist_url) {
        target.innerHTML = `
          <p><strong>${data.band_count}</strong> bands added to playlist.</p>
          <a href="${data.playlist_url}" target="_blank" rel="noopener" class="btn-primary">
            Open in Spotify
          </a>`;
      }
    } catch (_) {}
  }
});

document.body.addEventListener('htmx:beforeRequest', function (evt) {
  var form = evt.detail.elt;
  if (form.tagName === 'FORM') form.classList.add('form-submitting');
});

document.body.addEventListener('htmx:afterRequest', function (evt) {
  var form = evt.detail.elt;
  if (form.tagName === 'FORM') form.classList.remove('form-submitting');
});
