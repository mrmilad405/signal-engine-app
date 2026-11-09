# اپ موتور سیگنال — قدم ۲

این پوشه **فقط لایهٔ اپ** است. `engine/`, `web/dashboard.html`, `server.py`,
`web/windows.html` و `dashboard_standalone.html` هیچکدام تغییر نکردهاند.

## اجرای محلی

```bash
cd /home/user/app
python3 -m http.server 8001 --bind 0.0.0.0
```

بعد برو به `http://localhost:8001/`.  
روی گوشی اندروید، سرویسورکر/PWA فقط روی **HTTPS** یا `localhost` کار میکند؛
برای نصب واقعی همین پوشه را روی GitHub Pages / Cloudflare Pages بگذار.

## چی ساخته شد

| فایل | کار |
|---|---|
| `index.html` | همان ۶ پنجره + نوار پایین + بیاسکرولی، ولی بهجای `__DATA__` میخواند `data/latest.json` |
| `data/latest.json` | اسنپشات واقعی تازه «همان خروجی `build_payload()`» |
| `data/history/` | دو اسکن واقعی + `index.json` |
| `manifest.webmanifest` | نام، آیکون، `display:standalone` → قابلنصب روی اندروید |
| `icon-192.png`, `icon-512.png` | آیکون اپ |
| `sw.js` | کش آخرین فایل + هندلر `push` و `notificationclick` |
| دکمهٔ «🔔 اجازهٔ اعلان» | یکبار اجازه میگیرد و یک نوتیف آزمایشی با آیکون خود اپ نشان میدهد |

## نکتهٔ مهم

قدم ۳ هم **انجام شد**، ولی بدون VAPID / FCM / توکن / سرور:
`repo/.github/workflows/scan.yml` هر ۵ دقیقه `latest.json` را تازه میکند و سرویسورکر
با `periodicsync` + `check-signals` سیگنال تازه را پیدا کرده، با `showNotification()`
و آیکون خود اپ نشان میدهد. وقتی اپ باز است، همان چک هر ۴۵ ثانیه هم تکرار میشود.

دکمههای «شورت / جهان / momz» در این نسخهٔ استاتیک فقط نمایشیاند، چون
دیگر `/api/scan` صدا نمیزنند؛ دکمهٔ «↻ اسکن» همان `latest.json` را دوباره میخواند.

## وضعیتها

| چراغ | معنی |
|---|---|
| سبز | `latest.json` تازه خوانده شد |
| کهربایی | فایل قدیمی / شبکه نبود ولی کش موجود است |
| خاکستری | کش آفلاین |
| قرمز | خطای واقعی بروزرسانی |

## قدم ۳ «انجام شد»

همین اپ حالا در `repo/docs/` قرار دارد تا GitHub Pages از پوشهٔ `/docs` سروش کند.

- `.github/workflows/scan.yml` هر ۵ دقیقه `scripts/publish_snapshot.py` را اجرا میکند.
- اسکریپت همان `engine/` دستنخورده + `ccxt==4.5.92` واقعی را صدا میزند و
  `docs/data/latest.json` + تاریخچه را مینویسد و commit میکند.
- سرویسورکر علاوه بر `push`، هندلر **`periodicsync`** دارد: حتی وقتی اپ بسته است،
  هر ۵ دقیقه `latest.json` را چک میکند و اگه `signal_id` تازهای دید،
  با `registration.showNotification()` و آیکون خود اپ نوتیف میدهد.
- پس نیازی به VAPID / FCM / توکن بات / تلگرام / سرور نیست.
- روی اندروید بعد از «افزودن به صفحهٔ اصلی» و زدن دکمهٔ «🔔 اجازهٔ اعلان»،
  برچسب دکمه «· پسزمینه» میگیرد یعنی Periodic Background Sync ثبت شده است.

جزئیات استقرار: `repo/README.md`.
