export function formatCount(value) {
  if (!Number.isSafeInteger(value) || value < 0) return '—';
  return new Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: 1 }).format(value).replace('K', 'k');
}

export function isFresh(timestamp, maxAge, now = Date.now()) {
  const age = now - Date.parse(timestamp);
  return Number.isFinite(age) && age >= -60_000 && age <= maxAge;
}

export function statView(metric, kind, now = Date.now()) {
  const valid = Number.isSafeInteger(metric?.total) && metric.total >= 0;
  const live = valid && isFresh(metric.updated_at, kind === 'downloads' ? 5 * 60_000 : 3 * 60_000, now);
  return { text: formatCount(metric?.total), live,
    title: valid ? `${metric.total.toLocaleString('en-US')} ${kind === 'downloads' ? 'binary downloads' : 'model requests'}${kind === 'requests' && metric.since ? ` since ${new Date(metric.since).toLocaleDateString()}` : ''} · ${live ? 'updated' : 'last updated'} ${new Date(metric.updated_at).toLocaleString()}` : 'Count temporarily unavailable' };
}

const root = typeof document !== 'undefined' && document.querySelector('.usage');
if (root) {
  let previous;
  let running = false;
  let timer;
  function render(data, failed = false) {
    for (const kind of ['downloads', 'requests']) {
      const element = root.querySelector(`[data-stat="${kind}"]`);
      const view = statView(data?.[kind], kind);
      element.querySelector('strong').textContent = view.text;
      element.querySelector('.stat-label').textContent = kind === 'downloads' ? (data?.[kind]?.total === 1 ? 'download' : 'downloads') : (data?.[kind]?.total === 1 ? 'request processed' : 'requests processed');
      element.title = view.title;
      element.classList.toggle('is-live', view.live && !failed);
      element.querySelector('.stat-state').textContent = view.live && !failed ? 'Live total: ' : 'Last known total: ';
    }
  }
  async function refresh() {
    clearTimeout(timer);
    if (document.hidden || running) return;
    running = true;
    try {
      const result = await fetch('/v1/stats', { cache: 'no-store', credentials: 'omit', referrerPolicy: 'no-referrer', signal: AbortSignal.timeout(8000) });
      if (!result.ok) throw new Error('Statistics unavailable');
      const data = await result.json();
      for (const key of ['downloads', 'requests']) {
        if (data[key]?.total !== null && (!Number.isSafeInteger(data[key]?.total) || data[key].total < 0)) throw new Error('Invalid count');
      }
      previous = data;
      render(data);
    } catch { render(previous, true); }
    finally {
      running = false;
      if (!document.hidden) timer = setTimeout(refresh, 30_000);
    }
  }
  document.addEventListener('visibilitychange', () => {
    clearTimeout(timer);
    if (!document.hidden) refresh();
  });
  window.addEventListener('pagehide', () => clearTimeout(timer));
  window.addEventListener('pageshow', event => { if (event.persisted) refresh(); });
  refresh();
}
