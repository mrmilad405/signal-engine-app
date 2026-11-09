"""Scanner orchestrator: pulls the universe, evaluates every symbol and
returns a JSON-ready payload for the dashboard / API."""
from __future__ import annotations

import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from . import context as C, data as D, indicators as I
from .strategy import CFG, Config, evaluate


def _one(symbol: str, want_chart: bool, cfg: Config) -> dict[str, Any]:
    t0 = time.time()
    tfs, idx = C.load_live(symbol)
    res = evaluate(tfs, idx, symbol, cfg)
    res["symbol"] = symbol
    res["latency_ms"] = int((time.time() - t0) * 1000)
    if want_chart:
        res["chart"] = C.snapshot_context(tfs, idx)
    return res


def scan(symbols: list[str], charts_for: int = 6, cfg: Config = CFG,
         workers: int = 4, deep: bool = True) -> dict[str, Any]:
    """Scan symbols. ``deep`` adds chart payloads for the best signals."""
    t0 = time.time()
    results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_one, s, False, cfg): s for s in symbols}
        for f in as_completed(futs):
            sym = futs[f]
            try:
                results.append(f.result())
            except Exception as e:
                errors.append({"symbol": sym, "error": f"{type(e).__name__}: {e}"[:200]})

    results.sort(key=lambda r: (-int(bool(r.get("signal"))), -float(r.get("score") or 0)))

    if deep:
        sigs = [r for r in results if r.get("signal")]
        pool = sigs if sigs else [r for r in results if r.get("score") or r.get("atr_pct")]
        top = [r["symbol"] for r in pool[:charts_for]] or [r["symbol"] for r in results[:charts_for]]
        for r in results:
            if r["symbol"] in top:
                try:
                    tfs, idx = C.load_live(r["symbol"])
                    r["chart"] = C.snapshot_context(tfs, idx)
                    r["paper"] = _paper_trade(r)
                except Exception as e:
                    r["chart_error"] = str(e)[:120]

    signals = [r for r in results if r.get("signal")]
    return I.to_py({
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "elapsed_s": round(time.time() - t0, 1),
        "universe_size": len(symbols),
        "config": {"min_score": cfg.min_score, "min_rr": cfg.min_rr,
                   "risk_budget_pct": cfg.risk_budget_pct},
        "stats": {
            "longs": sum(1 for r in signals if r.get("direction") == "LONG"),
            "shorts": sum(1 for r in signals if r.get("direction") == "SHORT"),
            "avg_score": round(sum(r["score"] for r in signals) / len(signals), 1) if signals else 0,
            "avg_rr": round(sum(r["rr"] for r in signals) / len(signals), 2) if signals else 0,
        },
        "signals": signals,
        "watchlist": [r for r in results if not r.get("signal")],
        "errors": errors,
    })


def _paper_trade(r: dict) -> dict | None:
    """Levels already triggered? Show how far price has travelled since entry."""
    if not r.get("signal"):
        return None
    price = r["price"]
    entry = r["entry"]
    sl = r["sl"]
    risk = abs(entry - sl)
    d = 1 if r["direction"] == "LONG" else -1
    moved = (price - entry) * d
    return {"open_r": round(moved / risk, 2) if risk else 0.0}


if __name__ == "__main__":
    import json, sys

    n = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    syms = D.top_symbols(n)
    out = scan(syms, charts_for=3)
    print(json.dumps({k: v for k, v in out.items() if k != "signals"}, ensure_ascii=False)[:600])
    for s in out["signals"][:10]:
        print(f"{s['symbol']:18} {s['direction']:5} {s['setup']:16} score={s['score']:5} "
              f"RR={s['rr']} lev={s['leverage']}x  {s['grade']}")
    for w in out["watchlist"][:10]:
        print(f"  - {w['symbol']:18} {w.get('veto')} bias={w.get('bias')}")
    print("ERRORS", out["errors"])
