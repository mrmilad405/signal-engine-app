# موتور سیگنال → PWA بدون سرور

این ریپو خروجی «قدم ۳» است: همان موتور shipped، بدون تغییر، ولی بهصورت اپ استاتیک
روی GitHub Pages + یه زنگ ساعت رایگان GitHub Actions.

## ساختار

| پوشه | محتوا |
|---|---|
| `engine/` | کپی بایت‑به‑بایت ماژولهای shipped «دستنخورده» |
| `work/bt_final.json` | همان فایل بکتست shipped |
| `scripts/snapshot_server.py` | همان لایهٔ ساخت payload، فقط مسیرها عوض شده |
| `scripts/publish_snapshot.py` | اجرای موتور + نوشتن `docs/data/latest.json` و تاریخچه |
| `docs/` | اپ PWA «Pages از همین پوشه سرو میشود» |
| `.github/workflows/scan.yml` | هر ۵ دقیقه اجرا میشود و فایلها را commit میکند |

## راهاندازی «یکبار»

1. همین پوشه را به یه ریپوی تازهٔ GitHub پوش کن.
2. **Settings → Pages**:
   - Source: `Deploy from a branch`
   - Branch: `main` / Folder: **`/docs`**
   - Save.
3. آدرس Pages را روی گوشی اندروید با Chrome باز کن.
4. منوی Chrome → **«افزودن به صفحهٔ اصلی / Install app»**.
5. دکمهٔ **«🔔 اجازهٔ اعلان»** را بزن و اجازه بده.

## بعدش چی میشود؟

```text
GitHub Actions «هر ۵ دقیقه»
   → python scripts/publish_snapshot.py
   → همان engine/ بدون تغییر + ccxt واقعی bitget→mexc
   → docs/data/latest.json + docs/data/history/
   → git push
   → GitHub Pages فایل تازه را سرو میکند
   → Periodic Background Sync در سرویسورکر اپ
   → اگه signal_id تازهای بود: self.registration.showNotification(...)
   → نوتیف با آیکون خود اپ، حتی وقتی اپ بسته است
```

هیچ VAPID، هیچ FCM، هیچ توکن بات، هیچ سرور، هیچ Telegram و هیچ سرویس خارجی لازم نیست.

## نکتهها

- `cron: '*/5 * * * *'` حداقل فاصلهٔ گیتھاب است؛ گاهی چند دقیقه تأخیر دارد.
- **Periodic Background Sync** روی Chrome اندروید برای PWA نصبشده کار میکند و به
  «engagement» سایت وابسته است؛ ممکن است مرورگر بعضی چکها را عقب بیندازد. در
  پیشنمایش دسکتاپ کروم ممکن است این مجوز `denied` شود «طبیعی است». این آگاهانه
  بهجای Push سروری انتخاب شد چون گفتی سرور نداری.
- وقتی اپ باز است، همان `latest.json` هر ۴۵ ثانیه چک میشود و همان چک اعلان را هم
  با پیام `check-signals` به سرویسورکر میفرستد؛ بنابراین نوتیف هنگام بازبودن اپ قطعی است.
- خلاصهٔ ساده و چکلیست استقرار: `STEP3.md`.
- اگه روزی خواستی Push واقعی سروری/VAPID اضافه شود، `sw.js` از قبل هندلر `push`
  و `notificationclick` را دارد؛ فقط فرستنده اضافه میشود.
- `engine/` و `web/dashboard.html` نسخهٔ shipped لمس نشدند؛ `requirements.txt`
  همان `ccxt==4.5.92` + numpy/pandas را نصب میکند.

## اجرای محلی

```bash
pip install -r requirements.txt
python scripts/publish_snapshot.py
cd docs && python3 -m http.server 8001
```
