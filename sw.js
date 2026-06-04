/* WC26 sweepstake service worker — notifications only (no fetch/caching, so no stale-app risk). */
self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));

// Tapping a notification focuses the tracker (or opens it).
self.addEventListener('notificationclick', e => {
  e.notification.close();
  e.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(cs => {
    for (const c of cs) { if (c.url.indexOf('/tracker') !== -1 && 'focus' in c) return c.focus(); }
    if (self.clients.openWindow) return self.clients.openWindow('/tracker');
  }));
});

// Groundwork for offline Web Push: a push from the server shows a notification even with the tab closed.
self.addEventListener('push', e => {
  let d = { title: 'WC26 Sweepstake', body: 'Update' };
  try { if (e.data) d = e.data.json(); } catch (_) {}
  e.waitUntil(self.registration.showNotification(d.title || 'WC26 Sweepstake',
    { body: d.body || '', tag: d.tag || 'wc26', renotify: true }));
});
