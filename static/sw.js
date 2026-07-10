const CACHE = 'pando-v3';   // v3:PWA 改名 Pando + 图标换新文件名,bump 版本清掉旧缓存
const PRECACHE = ['/', '/manifest.json'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(PRECACHE)));
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// 通用推送转发(feat-frontend-plugin-arch):sw 不认识任何业务字段,
// 只把 payload 原样展示成通知;点击时把 data 整体 postMessage 给已打开页面
// (页面侧经 Pando 事件总线转发给插件),没有打开的页面则带 ?push=<json> 参数打开。
self.addEventListener('push', e => {
  let data = { title: 'Pando', body: '有一条新消息' };
  if (e.data) {
    try { data = e.data.json(); } catch { data.body = e.data.text(); }
  }
  e.waitUntil(
    self.registration.showNotification(data.title || 'Pando', {
      body: data.body || '',
      icon: '/icon-192.png?v=pando3',
      badge: '/icon-192.png?v=pando3',
      tag: 'pando-push',
      renotify: true,
      data,
    })
  );
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  const data = e.notification.data || {};
  e.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(list => {
      for (const c of list) {
        if (c.url && 'focus' in c) {
          c.postMessage({ type: 'push_click', data });
          return c.focus();
        }
      }
      if (clients.openWindow) {
        let url = '/';
        try { url = '/?push=' + encodeURIComponent(JSON.stringify(data)); } catch {}
        return clients.openWindow(url);
      }
    })
  );
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  // Don't cache live API / WS requests; plugin assets 交给浏览器 HTTP 缓存
  // (服务端 no-cache + manifest 版本参数负责失效,sw 不再兜一层旧副本)
  if (url.pathname.startsWith('/ws') ||
      url.pathname.startsWith('/sessions') ||
      url.pathname.startsWith('/memory') ||
      url.pathname.startsWith('/api/') ||
      url.pathname.startsWith('/plugin-assets/') ||
      url.pathname.startsWith('/health')) {
    return;
  }
  e.respondWith(
    fetch(e.request).then(r => {
      const clone = r.clone();
      caches.open(CACHE).then(c => c.put(e.request, clone));
      return r;
    }).catch(() => caches.match(e.request))
  );
});
