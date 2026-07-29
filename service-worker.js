const CACHE_NAME = 'travel-app-v1';
const ASSETS_TO_CACHE = [
  '/',
  'https://cdn-icons-png.flaticon.com/512/854/854878.png'
];

// 1. התקנת ה-Service Worker ושמירת נכסים בסיסיים ב-Cache
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      console.log('[PWA] Caching initial assets');
      return cache.addAll(ASSETS_TO_CACHE);
    })
  );
  self.skipWaiting();
});

// 2. הפעלת ה-Service Worker וניקוי גירסאות ישנות במידת הצורך
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((cacheNames) => {
      return Promise.all(
        cacheNames.map((cache) => {
          if (cache !== CACHE_NAME) {
            console.log('[PWA] Clearing old cache:', cache);
            return caches.delete(cache);
          }
        })
      );
    })
  );
  self.clients.claim();
});

// 3. תפיסת בקשות רשת (Fetch) – החזרת מידע מ-Cache כשאין אינטרנט
self.addEventListener('fetch', (event) => {
  // התעלמות מבקשות WebSocket של Streamlit ברקע כדי לא להפריע לתקשורת פנימית
  if (event.request.url.includes('_stcore/stream')) {
    return;
  }

  event.respondWith(
    fetch(event.request)
      .then((response) => {
        // אם החיבור מצליח – שמירת עותק עדכני ב-Cache והחזרת התגובה
        if (event.request.method === 'GET' && response.status === 200) {
          const responseClone = response.clone();
          caches.open(CACHE_NAME).then((cache) => {
            cache.put(event.request, responseClone);
          });
        }
        return response;
      })
      .catch(() => {
        // אם אין חיבור אינטרנט – משיכת המשאב מזיכרון ה-Cache המקומי
        return caches.match(event.request);
      })
  );
});