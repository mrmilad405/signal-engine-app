"""Event-driven backtester.

The same :func:`engine.strategy.evaluate` used live is replayed bar by bar on
long history (higher timeframes are reconstructed by resampling the base frame
and mapped to the *last closed* HTF bar), so the reported statistics describe
exactly the strategy the dashboard trades.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from . import context as C, data as D, indicators as I
from .strategy import CFG, Config, evaluate


@dataclass
class BTConfig:
    base_tf: str = "1h"
    bars: int = 6000
    warmup: int = 300
    max_hold: int = 96          # 96 x 15m = 24h
    cooldown: int = 2
    fee_maker_pct: float = 0.02   # limit entry
    fee_taker_pct: float = 0.06   # stop / market exit
    partials: tuple = (1.0,)
    use_tps: bool = False         # trailing exit is the validated default
    trail: str = "atr"            # "be" | "atr" | "none"
    atr_trail_mult: float = 3.0
    entry_tf: str = "1h"


def fetch_history(symbol: str, bars: int, tf: str = "1h") -> pd.DataFrame:
    return I.enrich(D.fetch_ohlcv(symbol, tf, bars, drop_partial=True))


def run_trade(base: pd.DataFrame, fill_idx: int, entry: float, sl0: float,
              tps: list[float], d: int, cfg: BTConfig,
              no_tp_first: bool = False) -> dict[str, Any]:
    """Manage one position bar by bar. Partials at TP1/2/3, configurable trail."""
    h = base["high"].to_numpy()
    l = base["low"].to_numpy()
    c = base["close"].to_numpy()
    atr = base["atr"].to_numpy()
    n = len(base)
    risk = abs(entry - sl0)
    if risk <= 0:
        return {"r": 0.0, "exit": "invalid", "bars": 0, "mfe": 0.0, "mae": 0.0,
                "hits": 0, "risk": risk}
    fracs = list(cfg.partials)
    remaining, realized = 1.0, 0.0
    hit = [False, False, False]
    cur = sl0
    best = worst = entry
    exit_reason, exit_idx = "timeout", min(n - 1, fill_idx + cfg.max_hold)
    last = min(n - 1, fill_idx + cfg.max_hold)

    for j in range(fill_idx, min(n, fill_idx + 1 + cfg.max_hold)):
        best = max(best, h[j]) if d > 0 else min(best, l[j])
        worst = min(worst, l[j]) if d > 0 else max(worst, h[j])
        if (l[j] <= cur) if d > 0 else (h[j] >= cur):
            realized += remaining * ((cur - entry) * d / risk)
            exit_reason = "breakeven" if abs(cur - entry) <= entry * 1e-7 else "stop"
            exit_idx = j
            remaining = 0.0
            break
        if no_tp_first and j == fill_idx:
            continue
        for k in (range(3) if cfg.use_tps else ()):  # type: ignore[union-attr]
            if hit[k]:
                continue
            if not ((h[j] >= tps[k]) if d > 0 else (l[j] <= tps[k])):
                break
            hit[k] = True
            f = min(remaining, fracs[k])
            realized += f * ((tps[k] - entry) * d / risk)
            remaining -= f
            if cfg.trail == "be":
                cur = entry if k == 0 else tps[0]
            elif cfg.trail == "atr" and np.isfinite(atr[j]):
                cand = best - d * cfg.atr_trail_mult * atr[j]
                cur = max(cur, cand) if d > 0 else min(cur, cand)
            if remaining <= 1e-9:
                exit_reason = f"tp{k+1}"
                exit_idx = j
                break
        if remaining <= 1e-9:
            break

    if remaining > 1e-9:
        realized += remaining * ((c[exit_idx] - entry) * d / risk)
        exit_reason = "timeout"
    return {"r": realized, "exit": exit_reason, "bars": int(exit_idx - fill_idx),
            "mfe": (best - entry) * d / risk, "mae": (worst - entry) * d / risk,
            "hits": sum(hit), "risk": risk}


def simulate(symbol: str, base: pd.DataFrame, cfg: BTConfig = BTConfig(),
             scfg: Config = CFG, fill_log: list | None = None) -> list[dict[str, Any]]:
    states, idx = C.load_from_base(base, cfg.base_tf)
    st = states[cfg.entry_tf]
    o, h, l, c = (base[k].to_numpy() for k in ("open", "high", "low", "close"))
    atr = base["atr"].to_numpy()
    times = base.index
    n = len(base)

    trades: list[dict[str, Any]] = []
    i = cfg.warmup
    while i < n - 2:
        ctx = {tf: int(arr[i]) for tf, arr in idx.items()}
        sig = evaluate(states, ctx, symbol, scfg)
        if not sig.get("signal"):
            i += 1
            continue

        d = 1 if sig["direction"] == "LONG" else -1
        sl0 = float(sig["sl"])
        tps = [float(t["price"]) for t in sig["tps"]]
        order = sig.get("order_type", "MARKET")
        entry_order = float(sig["entry"])
        fill_idx = i + 1
        no_tp_first = False

        if order == "LIMIT":
            fill_idx = -1
            for j in range(i + 1, min(n, i + 1 + int(sig.get("expiry_bars", 10)))):
                if (d > 0 and c[j] < sl0) or (d < 0 and c[j] > sl0):
                    break                                   # setup invalidated
                if (d > 0 and l[j] <= entry_order) or (d < 0 and h[j] >= entry_order):
                    fill_idx = j
                    break
            if fill_idx < 0:
                i += 1
                continue
            entry = entry_order
            no_tp_first = True
        else:
            entry = o[i + 1]
            if (d > 0 and entry <= sl0) or (d < 0 and entry >= sl0):
                i += 1
                continue

        res = run_trade(base, fill_idx, entry, sl0, tps, d, cfg, no_tp_first)
        risk = res["risk"]
        if risk <= 0:
            i += 1
            continue
        fee_pct = cfg.fee_maker_pct if order == "LIMIT" else cfg.fee_taker_pct
        fee_abs = entry * (fee_pct + cfg.fee_taker_pct) / 100.0
        tr = {
            "symbol": symbol, "dir": sig["direction"], "setup": sig["setup"],
            "score": sig["score"], "grade": sig["grade"], "rr_plan": sig["rr"],
            "order": order, "r": round(res["r"] - fee_abs / risk, 4),
            "bars": res["bars"], "exit": res["exit"],
            "entry_time": str(times[fill_idx]),
            "entry": round(float(entry), 8), "sl": sl0,
            "leverage": sig["leverage"],
            "mfe_r": round(res["mfe"], 3), "mae_r": round(res["mae"], 3),
            "hits": res["hits"],
            "sl_atr": round(risk / atr[fill_idx], 2) if np.isfinite(atr[fill_idx]) else None,
            "parts": sig.get("parts", {}), "feat": sig.get("feat", {}),
            "f_d1": (sig.get("feat") or {}).get("d1"),
            "f_h4": (sig.get("feat") or {}).get("h4"),
            "f_adx1h": (sig.get("feat") or {}).get("adx1h"),
            "f_momz": (sig.get("feat") or {}).get("momz"),
            "f_pos": (sig.get("feat") or {}).get("pos"),
            "bias": sig.get("bias"), "atr_pct": sig.get("atr_pct"),
            "pos": (sig.get("range") or {}).get("pos"),
        }
        trades.append(tr)
        if fill_log is not None:
            fill_log.append(tr)
        i = res["bars"] + fill_idx + cfg.cooldown

    return trades


def stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
    if not trades:
        return {"trades": 0}
    rs = np.array([t["r"] for t in trades], dtype=float)
    wins = rs[rs > 0]
    losses = rs[rs <= 0]
    eq = np.cumsum(rs)
    peak = np.maximum.accumulate(eq)
    dd = eq - peak
    gp, gl = float(wins.sum()), float(-losses.sum())

    def group(key):
        out: dict[str, dict[str, float]] = {}
        for t in trades:
            g = out.setdefault(str(t.get(key)), {"n": 0, "r": 0.0, "w": 0})
            g["n"] += 1
            g["r"] += t["r"]
            g["w"] += 1 if t["r"] > 0 else 0
        for g in out.values():
            g["avg_r"] = round(g["r"] / g["n"], 3)
            g["wr"] = round(g["w"] / g["n"] * 100, 1)
            g["r"] = round(g["r"], 2)
            del g["w"]
        return dict(sorted(out.items()))

    return I.to_py({
        "trades": int(len(rs)),
        "win_rate": round(float((rs > 0).mean() * 100), 1),
        "total_r": round(float(rs.sum()), 2),
        "expectancy_r": round(float(rs.mean()), 3),
        "profit_factor": round(gp / gl, 2) if gl > 0 else None,
        "avg_win_r": round(float(wins.mean()), 2) if len(wins) else 0.0,
        "avg_loss_r": round(float(losses.mean()), 2) if len(losses) else 0.0,
        "max_dd_r": round(float(dd.min()), 2),
        "sharpe_trade": round(float(rs.mean() / rs.std()), 2) if rs.std() > 0 else None,
        "avg_bars": round(float(np.mean([t["bars"] for t in trades])), 1),
        "longs": sum(1 for t in trades if t["dir"] == "LONG"),
        "shorts": sum(1 for t in trades if t["dir"] == "SHORT"),
        "avg_score": round(float(np.mean([t["score"] for t in trades])), 1),
        "limit_share": round(float(np.mean([t["order"] == "LIMIT" for t in trades]) * 100), 1),
        "by_grade": group("grade"),
        "by_setup": group("setup"),
        "by_dir": group("dir"),
        "by_order": group("order"),
        "exits": {k: sum(1 for t in trades if t["exit"] == k)
                  for k in sorted({t["exit"] for t in trades})},
        "equity_r": [round(float(x), 2) for x in eq[:: max(1, len(eq) // 120)]],
    })


def run(symbols: list[str], bt: BTConfig = BTConfig(), scfg: Config = CFG,
        log=print) -> dict[str, Any]:
    t0 = time.time()
    all_trades: list[dict[str, Any]] = []
    per_symbol: list[dict[str, Any]] = []
    period: dict[str, str] = {}
    for s in symbols:
        try:
            base = fetch_history(s, bt.bars, bt.base_tf)
            if len(base) < bt.warmup + 200:
                log(f"[bt] {s}: not enough history ({len(base)})")
                continue
            tr = simulate(s, base, bt, scfg)
            all_trades.extend(tr)
            st = stats(tr)
            per_symbol.append({"symbol": s, "trades": st.get("trades", 0),
                               "r": st.get("total_r", 0), "win_rate": st.get("win_rate", 0),
                               "expectancy_r": st.get("expectancy_r", 0)})
            period[s] = f"{base.index[0]:%Y-%m-%d}..{base.index[-1]:%Y-%m-%d}"
            log(f"[bt] {s:16} bars={len(base):5} trades={len(tr):4} "
                f"R={sum(t['r'] for t in tr):7.2f} exp={st.get('expectancy_r', 0):+.3f}")
        except Exception as e:
            log(f"[bt] {s}: ERROR {type(e).__name__}: {e}"[:160])

    overall = stats(all_trades)
    overall.update({
        "period": {"from": min(period.values())[:10] if period else None,
                   "to": max(period.values())[-10:] if period else None},
        "elapsed_s": round(time.time() - t0, 1),
        "config": {"bars": bt.bars, "base_tf": bt.base_tf, "max_hold": bt.max_hold,
                   "min_score": scfg.min_score, "min_rr": scfg.min_rr,
                   "fee_maker_pct": bt.fee_maker_pct, "fee_taker_pct": bt.fee_taker_pct,
                   "trail": bt.trail, "min_bias": scfg.min_bias,
                   "require_adx_4h": scfg.require_adx_4h, "prefer_limit": scfg.prefer_limit},
        "per_symbol": sorted(per_symbol, key=lambda x: -x["r"]),
        "symbol_count": len(per_symbol),
    })
    return I.to_py({"overall": overall, "trades": all_trades[-600:]})


if __name__ == "__main__":
    import json, sys

    syms = sys.argv[1].split(",") if len(sys.argv) > 1 else D.top_symbols(6)
    rep = run(syms)
    o = rep["overall"]
    print(json.dumps({k: o.get(k) for k in ("trades", "win_rate", "total_r", "expectancy_r",
                                            "profit_factor", "max_dd_r", "period", "elapsed_s",
                                            "limit_share")}, ensure_ascii=False))
    for s in o["per_symbol"]:
        print(f"{s['symbol']:16} n={s['trades']:4} R={s['r']:7.2f} WR={s['win_rate']:5}%")
