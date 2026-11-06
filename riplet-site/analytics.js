/* Anonymous counts only. No cookies, persistent IDs, or submitted video links. */
(() => {
  let visit = true;
  try {
    visit = sessionStorage.getItem('riplet-visited') !== '1';
    sessionStorage.setItem('riplet-visited', '1');
  } catch {}
  const base = window.RIPLET_CONFIG?.apiBase || 'https://api.riplet.cc:28934';
  fetch(`${base.replace(/\/+$/, '')}/api/analytics/pageview`, {
    method: 'POST', mode: 'cors', credentials: 'omit', keepalive: true,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ referrer: document.referrer, visit }),
  }).catch(() => {});
})();
