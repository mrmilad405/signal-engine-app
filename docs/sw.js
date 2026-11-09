/* Signal Engine PWA — step 2 */
const CACHE = 'signal-engine-v5';
const CORE = [
  './', './index.html', './manifest.webmanifest',
  './icon-192.png', './icon-512.png',
  './data/latest.json', './data/history/index.json'
];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(CORE)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  const isLatest = url.pathname.endsWith('/data/latest.json');

  if (isLatest) {
    e.respondWith(
      fetch(req).then(res => {
        const copy = res.clone();
        caches.open(CACHE).then(c => c.put(req, copy));
        return res;
      }).catch(() => caches.match(req).then(hit => hit || caches.match('./index.html')))
    );
    return;
  }
  e.respondWith(
    caches.match(req).then(hit => hit || fetch(req).then(res => {
      const copy = res.clone();
      caches.open(CACHE).then(c => c.put(req, copy));
      return res;
    }).catch(() => caches.match('./index.html')))
  );
});

/* Remote push arrives here in step 3; the app already knows how to show it. */
self.addEventListener('push', e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (_) { d = { body: e.data && e.data.text() }; }
  const title = d.title || '🟢 سیگنال جدید';
  const body  = d.body  || 'TP1 / TP2 / TP3 را در اپ ببین.';
  e.waitUntil(self.registration.showNotification(title, {
    body, icon:'./icon-192.png', badge:'./icon-192.png',
    tag: d.tag || 'signal-engine', data:{ url: d.url || './?win=signals' }
  }));
});
self.addEventListener('notificationclick', e => {
  e.notification.close();
  const target = (e.notification.data && e.notification.data.url) || './?win=signals';
  e.waitUntil(clients.matchAll({type:'window',includeUncontrolled:true}).then(list => {
    for (const c of list) if ('focus' in c) return c.focus().then(() => c.navigate && c.navigate(target));
    if (clients.openWindow) return clients.openWindow(target);
  }));
});

/* ---------- step 3: background check without any push server ---------- */
const STATE_CACHE = 'signal-engine-state';
const idOf = s => [s.symbol, s.ts, s.direction, s.entry].join('|');
async function seenIds(){
  try{
    const c=await caches.open(STATE_CACHE), r=await c.match('./seen-ids');
    return r ? await r.json() : [];
  }catch(e){ return []; }
}
async function setSeenIds(ids){
  try{
    const c=await caches.open(STATE_CACHE);
    await c.put('./seen-ids', new Response(JSON.stringify(ids),
      {headers:{'Content-Type':'application/json'}}));
  }catch(e){}
}
async function checkSignals(){
  let d;
  try{
    const res=await fetch('./data/latest.json',{cache:'no-store'});
    if(!res.ok) return;
    d=await res.json();
  }catch(e){ return; }
  const ids=(d.signals||[]).map(idOf), seen=await seenIds();
  const fresh=ids.filter(x=>!seen.includes(x));
  await setSeenIds(ids);
  if(!fresh.length) return;
  const g=(d.signals||[]).find(x=>idOf(x)===fresh[0]) || (d.signals||[])[0];
  if(!g) return;
  const tps=(g.tps||[]).map(t=>t.label+' '+t.price).join(' · ');
  await self.registration.showNotification(
    '🟢 سیگنال جدید | '+g.symbol.split('/')[0]+' | '+g.direction,
    { body:'زمان صدور '+d.generated_at+' · '+tps,
      icon:'./icon-192.png', badge:'./icon-192.png',
      tag:'signal-'+fresh[0], data:{url:'./?win=signals'} }
  );
}
self.addEventListener('periodicsync', e => {
  if(e.tag==='check-signals') e.waitUntil(checkSignals());
});
self.addEventListener('message', e => {
  if(e.data==='check-signals') e.waitUntil(checkSignals());
});
