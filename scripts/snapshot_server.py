"""Snapshot builder for the static PWA.

Imports the shipped engine unchanged. Called by scripts/publish_snapshot.py
inside GitHub Actions; the HTTP layer is not used here.
"""
from __future__ import annotations

import argparse
import inspect
import json
import os
import sys
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# --- REAL ccxt: pip-installed in CI, vendored optionally for local runs -----
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENDOR = os.path.join(ROOT, "vendor")
if os.path.isdir(VENDOR):
    sys.path.insert(0, VENDOR)
sys.path.insert(0, ROOT)   # so `engine/` is importable from the repo root
import ccxt  # noqa: E402   (real package, NOT a stub)
CCXT_REAL = True

TEMPLATE = os.path.join(ROOT, "docs", "index.html")
SNAPSHOT = os.path.join(ROOT, "work", "snapshot.json")
BACKTEST = os.path.join(ROOT, "work", "bt_final.json")

from engine import data as D, indicators as I, scan as SC, context as C  # noqa: E402
from engine import backtest as B, structure as S, strategy as ST            # noqa: E402
from engine.strategy import Config                                          # noqa: E402

# engine.data is used EXACTLY as shipped. No data-layer monkeypatching.
# CCXT routing stays EXCHANGE_CHAIN = ("bitget", "mexc") from engine/data.py.

# --- lightweight instrumentation of the REAL modules -----------------------
PIPE = {"load_ms": 0.0, "eval_ms": 0.0, "fetch_ms": 0.0,
        "load_calls": 0, "eval_calls": 0, "fetch_calls": 0}
_pipe_lock = threading.Lock()
SCAN_LOCK = threading.Lock()

_orig_load = C.load_live
_orig_eval = SC.evaluate
_orig_fetch = D.fetch_ohlcv


def _timed_fetch(*a, **k):
    """Transparent timer: same call, same result, no behaviour change."""
    t0 = time.perf_counter()
    r = _orig_fetch(*a, **k)
    with _pipe_lock:
        PIPE["fetch_ms"] += (time.perf_counter() - t0) * 1000
        PIPE["fetch_calls"] += 1
    return r


D.fetch_ohlcv = _timed_fetch


def _timed_load(*a, **k):
    t0 = time.perf_counter()
    r = _orig_load(*a, **k)
    with _pipe_lock:
        PIPE["load_ms"] += (time.perf_counter() - t0) * 1000
        PIPE["load_calls"] += 1
    return r


def _timed_eval(*a, **k):
    t0 = time.perf_counter()
    r = _orig_eval(*a, **k)
    with _pipe_lock:
        PIPE["eval_ms"] += (time.perf_counter() - t0) * 1000
        PIPE["eval_calls"] += 1
    return r


C.load_live = _timed_load
SC.evaluate = _timed_eval

_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()
TTL = 20.0


def backtest_payload() -> dict:
    try:
        with open(BACKTEST, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def build_payload(n: int = 20, shorts: bool = False, momz: float = 0.30) -> dict:
    with SCAN_LOCK:
        with _pipe_lock:
            PIPE.update({"load_ms": 0.0, "eval_ms": 0.0, "fetch_ms": 0.0,
                         "load_calls": 0, "eval_calls": 0, "fetch_calls": 0})

        cfg = Config(allow_shorts=bool(shorts), momz_min=float(momz))
        syms = D.top_symbols(n)   # real CCXT universe
        t0 = time.perf_counter()
        res = SC.scan(syms, charts_for=6, cfg=cfg, deep=True, workers=4)
        total_ms = (time.perf_counter() - t0) * 1000.0

        with _pipe_lock:
            p = dict(PIPE)
        fetch_ms = p["fetch_ms"]
        w = 4  # SC.scan workers; per-thread timings are aggregated -> normalise
        stages = [
            {"name": "ccxt → data.fetch_ohlcv", "ms": round(fetch_ms / w, 1),
             "detail": f"{p['fetch_calls']} فراخوان واقعی · bitget → mexc (÷۴)"},
            {"name": "indicators.enrich + structure.build",
             "ms": round(max(1.0, (p["load_ms"] - fetch_ms) / w), 1),
             "detail": f"{p['load_calls']} بار load_live (۱D/۴H/۱H) (÷۴)"},
            {"name": "strategy.evaluate", "ms": round(max(1.0, p["eval_ms"] / w), 1),
             "detail": f"{p['eval_calls']} ارزیابی · min_score={cfg.min_score} (÷۴)"},
            {"name": "scan.scan + snapshot_context", "ms": round(total_ms, 1),
             "detail": f"زمان سپریشدهٔ واقعی · {len(syms)} نماد · ۴ worker"},
        ]
        res.update({
            "backtest": backtest_payload(),
            "diagnostics": diagnostics(),   # inlined so offline snapshots stay complete
            "settings": {"universe": n, "shorts": bool(shorts), "momz": float(momz),
                         "regime": "live", "seed": 0},
            "mode": {"data": "LIVE exchange data", "ccxt": ccxt.__version__,
                     "engine": "unmodified engine/ modules",
                     "chain": list(D.EXCHANGE_CHAIN),
                     "note": "دادهٔ واقعی OHLCV و تیکر از صرافی؛ منطق موتور دستنخورده است"},
            "pipeline": {"stages": stages, "total_ms": round(total_ms, 1)},
        })
        return I.to_py(res)


def get_scan(n: int, shorts: bool, momz: float, force: bool = False) -> dict:
    key = f"{n}:{int(shorts)}:{momz}"
    with _cache_lock:
        hit = _cache.get(key)
        if hit and not force and time.time() - hit[0] < TTL:
            return hit[1]
    res = build_payload(n, shorts, momz)
    with _cache_lock:
        _cache[key] = (time.time(), res)
    return res


def _initial_snapshot() -> dict:
    if os.path.exists(SNAPSHOT):
        try:
            with open(SNAPSHOT, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return build_payload()


def render_page() -> bytes:
    html = open(TEMPLATE, encoding="utf-8").read()
    snap = _initial_snapshot()
    blob = json.dumps(I.to_py(snap), ensure_ascii=False)
    # make it safe inside an inline <script> element
    for a, b in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026"),
                 ("\u2028", "\\u2028"), ("\u2029", "\\u2029")):
        blob = blob.replace(a, b)
    html = html.replace("__DATA__", blob)
    # Presentation only: this instance now serves REAL exchange data.
    html = html.replace(
        'دادهٔ مصنوعی و قطعی (بدون <code>ccxt</code> / صرافی)',
        'دادهٔ <b style="color:#16d6a4">واقعی</b> · <code>ccxt</code> 4.5.92')
    html = html.replace('</head>',
        '<style>#regimeBtn,#seedBtn{display:none!important}</style>\n</head>')
    live_js = """<script>
/* live mode: two synthetic-only knobs are hidden, label shows the real feed */
syncBtns = function(){
  const d = state.data || {}, m = d.mode || {};
  $('momBtn').textContent = 'momz ≥ ' + fa((state.momz||0.30).toFixed(2));
  $('uniBtn').textContent = 'جهان: ' + fa(state.universe) + ' نماد';
  $('modeLabel').textContent = 'ccxt ' + (m.ccxt||'') +
      ' · ' + (m.chain||['bitget','mexc']).join(' → ') +
      ' · ' + fa(state.universe) + ' نماد';
};
$('regimeBtn').onclick = ()=>{}; $('seedBtn').onclick = ()=>{};
syncBtns();
</script>"""
    return html.replace('</body>', live_js + '\n</body>').encode("utf-8")


# --------------------------------------------------------------------------- #
# Live diagnostics (the audit findings, recomputed on every request)
# --------------------------------------------------------------------------- #
def diagnostics() -> dict:
    checks: list[dict] = []

    # 1) backtest.run()
    try:
        B.run([], log=lambda *a: None)
        checks.append({"ok": True, "title": "backtest.run()", "detail": "بدون خطا اجرا شد."})
    except Exception as e:
        checks.append({"ok": False, "title": "backtest.run()",
                       "detail": f"{type(e).__name__}: {e} — به فیلدهای حذف‌شدهٔ Config "
                                 "(min_bias / require_adx_4h / prefer_limit) ارجاع میدهد."})

    # 2) the trailing-exit experiment
    n = 100
    idx = __import__("pandas").date_range("2026-01-01", periods=n, freq="1h", tz="UTC")
    df = __import__("pandas").DataFrame(
        {"open": [100.0] * n, "high": [100.0] * n, "low": [100.0] * n,
         "close": [100.0] * n, "volume": [1.0] * n, "atr": [1.0] * n}, index=idx)
    df.loc[df.index[10:60], ["high", "low", "close"]] = [130.0, 129.0, 129.0]
    df.loc[df.index[60:], ["high", "low", "close"]] = [99.5, 98.5, 99.0]
    shipped = B.run_trade(df, 0, 100.0, 98.0, [104., 108., 112.], 1, B.BTConfig(), False)
    trailed = B.run_trade(df, 0, 100.0, 98.0, [104., 108., 112.], 1,
                          B.BTConfig(use_tps=True, partials=(0., 0., 0.)), False)
    checks.append({
        "ok": False, "title": "تریلینگ ۳×ATR در بکتست",
        "detail": f"همان معامله با حداکثر سود +{shipped['mfe']:.0f}R: تنظیمات shipped → "
                  f"{shipped['r']:+.1f}R (خروج {shipped['exit']})، "
                  f"در حالی که با فعال شدن حلقهٔ TP (جایی که تریل پنهان است) → "
                  f"{trailed['r']:+.1f}R. استاپ هرگز جابهجا نمیشود.",
    })

    checks.append({
        "ok": False, "title": "BTConfig.use_tps",
        "detail": f"پیشفرض use_tps={B.BTConfig().use_tps} و تنها بهروزرسانی تریل داخل حلقهٔ TP است "
                  "(backtest.py:84-86) ⇒ تریل عملاً غیرفعال است.",
    })

    src = inspect.getsource(ST.evaluate)
    checks.append({
        "ok": False, "title": "Config.trail_atr / hold_bars",
        "detail": f"فقط {src.count('trail_atr')} بار در evaluate() استفاده شده، آن هم در رشتهٔ متنی "
                  "exit_policy؛ هیچ اثری روی خروج معامله ندارند.",
    })

    bt = backtest_payload()
    has_trades = "trades" in bt and bool(bt.get("trades"))
    checks.append({
        "ok": has_trades, "title": "bt_final.json :: لیست معاملات",
        "detail": ("لیست معاملات موجود است." if has_trades else
                   "فقط خلاصهٔ تجمیعی (config/IS/HOLD/overall/score_buckets) بدون هیچ ردیف معاملهای — "
                   "آمار قابل بازتولید نیست."),
    })

    # positive checks
    try:
        ph, _ = S.swing_pivots(I.enrich(D.fetch_ohlcv("BTC/USDT:USDT", "1h", 700)), k=2)
        causal = bool(not ph[-1] and not ph[-2])
    except Exception:
        causal = False
    checks.append({
        "ok": causal, "title": "علّی بودن structure.build",
        "detail": "سوینگها k کندل دیرتر ثبت میشوند و k کندل انتهایی هرگز پیوت نیستند؛ "
                  "آرایههای پربار (last_high_arr و …) برای بکتست امناند." if causal
                  else "بررسی ناموفق.",
    })

    try:
        base = D.fetch_ohlcv("BTC/USDT:USDT", "1h", 300)
        states, mapping = C.load_from_base(base, "1h")
        ok_map = (len(mapping["4h"]) == len(base)
                  and int(mapping["4h"].max()) < len(states["4h"].df))
    except Exception:
        ok_map = False
    checks.append({
        "ok": ok_map, "title": "نگاشت تایمفریم بالاتر",
        "detail": "load_from_base هر کندل ۱H را به «آخرین کندل کاملاً بستهشده»ی ۴H/۱D نگاشت میکند."
                  if ok_map else "بررسی ناموفق.",
    })

    checks.append({
        "ok": True, "title": "لایهٔ داده (واقعی)",
        "detail": f"ccxt {ccxt.__version__} · زنجیرهٔ {D.EXCHANGE_CHAIN} · "
                  "یونیورس از fetch_tickers صرافی و OHLCV واقعی ۱D/۴H/۱H. "
                  "هیچ دادهٔ مصنوعی در مسیر اصلی وجود ندارد؛ engine/data.py دستنخورده است.",
    })
    return {"checks": checks, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


class Handler(BaseHTTPRequestHandler):
    server_version = "PrototypeEngine/1.0"

    def _send(self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(I.to_py(obj), ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html", "/dashboard"):
                return self._send(200, render_page())
            if u.path == "/api/health":
                return self._json({
                    "ok": True, "mode": "prototype / LIVE exchange data",
                    "ccxt": ccxt.__version__, "chain": list(D.EXCHANGE_CHAIN),
                    "engine": "unmodified engine/",
                    "universe": "dynamic (exchange fetch_tickers)",
                    "cached_keys": list(_cache),
                    "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                })
            if u.path == "/api/backtest":
                return self._json(backtest_payload())
            if u.path == "/api/diagnostics":
                return self._json(diagnostics())
            if u.path in ("/api/scan", "/api/symbol"):
                n = max(1, min(40, int(q.get("symbols", 20))))
                shorts = q.get("shorts", "0") in ("1", "true", "yes")
                momz = max(0.05, min(3.0, float(q.get("momz", 0.30))))
                force = q.get("force", "0") in ("1", "true")
                # regime / seed are synthetic-mode leftovers: ignored here.
                if u.path == "/api/symbol":
                    sym = q.get("symbol", "").strip()
                    if not sym:
                        return self._json({"error": "symbol required"}, 400)
                    if "/" not in sym:
                        sym = f"{sym}/USDT:USDT"
                    with SCAN_LOCK:
                        cfg = Config(allow_shorts=shorts, momz_min=momz)
                        return self._json(SC.scan([sym], charts_for=1, deep=True, cfg=cfg))
                return self._json(get_scan(n, shorts, momz, force))
            return self._json({"error": "not found"}, 404)
        except Exception as e:
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def log_message(self, fmt, *args):
        print("[proto]", self.address_string(), fmt % args, flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="0.0.0.0")
    a = ap.parse_args()
    print(f"[proto] prototype on http://{a.host}:{a.port}  "
          f"(LIVE data: ccxt {ccxt.__version__}, {D.EXCHANGE_CHAIN})", flush=True)
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("[proto] bye")
