// HTMX response handler — pretty-print ingest results and refresh dashboard
document.body.addEventListener('htmx:afterSwap', function (evt) {
  const target = evt.detail.target;

  // Ingest result — render per-poster summary cards, then reload dashboard
  if (target.id === 'ingest-result') {
    try {
      const data = JSON.parse(target.textContent);
      const results = data.results || [];
      let html = '';
      let hasSuccess = false;

      for (const r of results) {
        if (r.error) {
          html += `<div class="ingest-error"><strong>${r.source || 'File'}</strong>: ${r.error}</div>`;
        } else {
          hasSuccess = true;
          const alpha  = r.alphabetical ? '<span class="tag">alphabetical</span>' : '';
          const action = r.reimport ? 're-imported' : 'ingested';
          const detail = r.reimport
            ? `${r.bands_added} new &middot; ${r.bands_merged} existing (scores &amp; notes kept)`
            : `${r.bands_added} new &middot; ${r.bands_merged} merged`;
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
        html += `<p class="muted" style="font-size:0.8rem;margin-top:0.5rem">Refreshing dashboard in 3 seconds&hellip;</p>`;
        setTimeout(() => location.reload(), 3000);
      }
      target.innerHTML = html || `<div class="ingest-error">No results returned.</div>`;
    } catch (_) {
      target.innerHTML = `<div class="ingest-error">${target.textContent}</div>`;
    }
  }

  // Research result
  if (target.id === 'research-status') {
    try {
      const data = JSON.parse(target.textContent);
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
      const data = JSON.parse(target.textContent);
      if (data.status === 'ok') {
        target.innerHTML = '<span style="color:var(--success)">&#10003; Rating saved.</span>';
      }
    } catch (_) {}
  }

  // Playlist result
  if (target.id === 'playlist-result') {
    try {
      const data = JSON.parse(target.textContent);
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
  const form = evt.detail.elt;
  if (form.tagName === 'FORM') form.classList.add('form-submitting');
});

document.body.addEventListener('htmx:afterRequest', function (evt) {
  const form = evt.detail.elt;
  if (form.tagName === 'FORM') form.classList.remove('form-submitting');
});
