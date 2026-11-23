// The URL fragment stays in the browser; only same-origin API calls carry the key.
(() => {
  const storageKey = 'telos-dashboard-access';
  const fragment = new URLSearchParams(location.hash.slice(1));
  let key = fragment.get('access');
  if (key !== null) {
    history.replaceState(null, '', location.pathname + location.search);
    if (!/^[0-9a-f]{64}$/.test(key)) key = null;
  }
  try {
    if (key) localStorage.setItem(storageKey, key);
    else key = localStorage.getItem(storageKey);
  } catch {
    // Storage may be unavailable in private browsing; access still works in this tab.
  }
  window.dashboardFetch = (path, options = {}) => {
    const url = new URL(path, location.origin);
    if (url.origin !== location.origin || !url.pathname.startsWith('/api/')) {
      throw new Error('Not a dashboard API path');
    }
    const headers = new Headers(options.headers);
    if (key) headers.set('Authorization', 'Bearer ' + key);
    return fetch(url, {...options, headers, redirect: 'error'});
  };
})();
