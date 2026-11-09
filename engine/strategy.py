"""Signal engine.

Design is evidence-driven. On 100 days of 15m data across 25 symbols the classic
"15m SMC entry" family measured ~-0.11R after fees (no edge), while a 1h
volatility-normalised momentum continuation setup with a 3xATR trailing exit
measured +0.13R in-sample, +0.14R out-of-sample and +0.27R on a completely
unseen holdout of 10 symbols. That family is the trigger; structure work (BOS /
CHoCH, order blocks, FVG, liquidity sweeps) is used for *context, ranking and
targets*, which is where it measurably helped.

Decision timeframe : 1H   (validated)
Context timeframes : 1D, 4H (bias / structure / POI)
Execution          : 15M, 5M (pullback limit entry, micro confirmation)

Trigger (all required):
    momentum_z = (close/close[-L] - 1) / (ATR% * sqrt(L))  >= momz_min
    ADX(14) >= adx_min
    EMA50 > EMA200          (regime)
    close >= EMA50          (short-term alignment)
Exit: initial SL = 2*ATR(1H); thereafter 3*ATR(1H) chandelier trail, max hold 96h.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from . import structure as S

TF_LADDER = ("1d", "4h", "1h", "15m", "5m")
BIAS_W = {"1d": 0.40, "4h": 0.35, "1h": 0.25}


@dataclass
class Config:
    # --- trigger (validated) -------------------------------------------
    look: int = 16                  # momentum lookback in 1H bars
    momz_min: float = 0.30          # vol-normalised momentum threshold
    adx_min: float = 20.0
    sl_atr: float = 2.0             # initial stop
    trail_atr: float = 3.0          # chandelier trail
    hold_bars: int = 96             # max 96 x 1H = 4 days
    # --- execution ------------------------------------------------------
    pullback_atr: float = 0.40      # limit below/above the signal close
    pullback_valid: int = 8         # 1H bars the resting order stays alive
    # --- risk -----------------------------------------------------------
    risk_budget_pct: float = 1.0    # % of equity risked per trade
    max_leverage: int = 20
    fee_pct: float = 0.10           # round-trip cost, % of notional
    # --- ranking --------------------------------------------------------
    min_score: float = 60.0
    allow_shorts: bool = False      # backtest: shorts -0.13R over 448 trades, longs +1.22R
    min_rr: float = 2.0
    atr_pct_min: float = 0.25        # below = dead market, fees eat the edge
    atr_pct_max: float = 4.0         # above = micro-cap noise, unusable stop
    zone_max_age: int = 200
    tp_r: tuple = (2.0, 4.0, 6.0)


CFG = Config()


# --------------------------------------------------------------------------- #
def _v(st: S.StructureState | None, key: str, i: int) -> float:
    if st is None:
        return float("nan")
    try:
        return float(st.df[key].iloc[i])
    except Exception:
        return float("nan")


def _rnd(p: float) -> float:
    if not np.isfinite(p):
        return p
    a = abs(p)
    if a >= 1000:
        return round(p, 2)
    if a >= 1:
        return round(p, 4)
    if a >= 0.01:
        return round(p, 5)
    return round(p, 8)


def momentum_read(st: S.StructureState, i: int, cfg: Config) -> dict[str, float]:
    c = st.df["close"].to_numpy()
    atr_pct = _v(st, "atr_pct", i) / 100.0
    L = cfg.look
    if i < L or not np.isfinite(atr_pct) or atr_pct <= 0:
        return {"mom": 0.0, "mom_z": 0.0, "ret": 0.0}
    ret = c[i] / c[i - L] - 1.0
    mom_z = ret / (atr_pct * np.sqrt(L))
    return {"mom": float(ret), "mom_z": float(mom_z), "ret": float(ret)}


def _scan_event(st: S.StructureState | None, i: int, direction: int, lookback: int):
    if st is None:
        return None
    for j in range(i, max(0, i - lookback) - 1, -1):
        if st.ev_dir[j] == direction and st.ev_kind[j]:
            return j, str(st.ev_kind[j]), float(st.ev_strength[j])
    return None


def _scan_sweep(st: S.StructureState | None, i: int, direction: int, lookback: int):
    if st is None:
        return None
    for j in range(i, max(0, i - lookback) - 1, -1):
        if st.sweep_dir[j] == direction and np.isfinite(st.sweep_level[j]):
            return j, float(st.sweep_level[j])
    return None


# --------------------------------------------------------------------------- #
def evaluate(tfs: dict[str, S.StructureState], idx: dict[str, int],
             symbol: str, cfg: Config = CFG) -> dict[str, Any]:
    dec = "1h"
    st = tfs.get(dec)
    if st is None or dec not in idx:
        return {"symbol": symbol, "signal": False, "veto": "no_data"}

    i = idx[dec]
    close = _v(st, "close", i)
    atr = _v(st, "atr", i)
    atr_pct = _v(st, "atr_pct", i)
    if not (np.isfinite(close) and np.isfinite(atr) and atr > 0):
        return {"symbol": symbol, "signal": False, "veto": "no_data"}

    if not (cfg.atr_pct_min <= atr_pct <= cfg.atr_pct_max):
        return {"symbol": symbol, "signal": False,
                "veto": "too_volatile" if atr_pct > cfg.atr_pct_max else "too_flat",
                "price": _rnd(close), "atr_pct": round(atr_pct, 2), "score": 0}

    ema20, ema50, ema200 = (_v(st, k, i) for k in ("ema20", "ema50", "ema200"))
    adx = _v(st, "adx", i)
    rsi = _v(st, "rsi", i)
    volz = _v(st, "vol_z", i)
    mom = momentum_read(st, i, cfg)
    momz = mom["mom_z"]

    # ---- multi-timeframe structure read (context, not a gate) ------------
    tf_rows = []
    bias = 0.0
    parts: dict[str, int] = {}
    for tf in ("1d", "4h", "1h"):
        s2 = tfs.get(tf)
        if s2 is None or tf not in idx:
            continue
        j = idx[tf]
        tr = int(s2.trend[j])
        parts[tf] = tr
        bias += BIAS_W[tf] * tr
        tf_rows.append({
            "tf": tf, "trend": tr,
            "rsi": round(_v(s2, "rsi", j), 1), "adx": round(_v(s2, "adx", j), 1),
            "atr_pct": round(_v(s2, "atr_pct", j), 2),
            "close": _rnd(_v(s2, "close", j)),
            "ema50": _rnd(_v(s2, "ema50", j)),
            "ema200": _rnd(_v(s2, "ema200", j)),
            "pos": round(S.eq_position(s2, j), 3),
        })

    # ---- trigger ---------------------------------------------------------
    regime_up = np.isfinite(ema50) and np.isfinite(ema200) and ema50 > ema200
    regime_dn = np.isfinite(ema50) and np.isfinite(ema200) and ema50 < ema200
    trig_long = (momz >= cfg.momz_min and regime_up and close >= ema50
                 and np.isfinite(adx) and adx >= cfg.adx_min)
    trig_short = (cfg.allow_shorts and -momz >= cfg.momz_min and regime_dn
                  and close <= ema50 and np.isfinite(adx) and adx >= cfg.adx_min)

    d = 1 if trig_long else (-1 if trig_short else 0)
    base_out = {
        "symbol": symbol, "price": _rnd(close), "bias": round(bias, 3),
        "bias_parts": parts, "tf_rows": tf_rows,
        "mom_z": round(momz, 2), "mom_pct": round(mom["mom"] * 100, 2),
        "atr_pct": round(atr_pct, 2), "adx": round(adx, 1) if np.isfinite(adx) else None,
    }
    if d == 0:
        if not (regime_up or regime_dn):
            veto = "no_regime"
        elif abs(momz) < cfg.momz_min:
            veto = "no_momentum"
        elif np.isfinite(adx) and adx < cfg.adx_min:
            veto = "weak_trend"
        else:
            veto = "not_aligned"
        return {**base_out, "signal": False, "veto": veto, "score": 0}

    # ---- structure / POI confluence (ranking only) ------------------------
    reasons: list[dict[str, Any]] = []
    warnings: list[str] = []

    ev4 = _scan_event(tfs.get("4h"), idx.get("4h", 0), d, 12) if "4h" in idx else None
    ev1 = _scan_event(st, i, d, 8)
    sw1 = _scan_sweep(st, i, d, 4)
    struct_pts = 0.0
    if ev4 is not None and ev4[1] == "BOS":
        struct_pts += 5.0
        reasons.append({"code": "bos4h", "fa": "BOS هم‌جهت روی 4 ساعته", "pts": 5.0})
    if ev1 is not None:
        struct_pts += 3.0
        reasons.append({"code": "bos1h",
                        "fa": f"{ev1[1]} روی 1 ساعته، {i - ev1[0]} کندل قبل", "pts": 3.0})
    if sw1 is not None:
        struct_pts += 2.0
        reasons.append({"code": "sweep", "fa": "سوییپ نقدینگی و بازگشت سریع", "pts": 2.0})

    poi = None
    poi_src = ""
    for tf in ("4h", "1h"):
        s2 = tfs.get(tf)
        if s2 is None or tf not in idx:
            continue
        cands = S.pois_near(s2, idx[tf], d, close, _v(s2, "atr", idx[tf]),
                            tol=1.4, max_age=cfg.zone_max_age)
        if cands:
            poi, poi_src = cands[0], tf
            break
    poi_pts = 0.0
    if poi is not None:
        poi_pts = 8.0 if poi.kind == "OB+FVG" else 6.0 if poi.kind == "OB" else 5.0
        if poi.contains(close):
            poi_pts += 2.0
        reasons.append({"code": "poi",
                        "fa": f"ناحیه {poi.kind} روی {poi_src} نزدیک قیمت", "pts": round(poi_pts, 1)})

    # ---- score -----------------------------------------------------------
    mom_pts = float(np.clip(abs(momz) / 1.2, 0, 1) * 30.0)
    reasons.insert(0, {"code": "momentum",
                       "fa": f"مومنتوم ۱۶ کندلی {mom['mom']*100:+.1f}٪ = {abs(momz):.2f}×ATR√n",
                       "pts": round(mom_pts, 1)})
    adx_pts = float(np.clip((adx - cfg.adx_min) / 25.0, 0, 1) * 15.0) if np.isfinite(adx) else 0.0
    reasons.append({"code": "adx", "fa": f"ADX {adx:.0f} — روند فعال", "pts": round(adx_pts, 1)})
    reg_pts = 10.0
    if parts.get("4h", 0) == d:
        reg_pts += 5.0
    if parts.get("1d", 0) == d:
        reg_pts += 5.0
    reasons.append({"code": "regime",
                    "fa": "EMA50>EMA200 و قیمت بالای EMA50" if d > 0 else
                          "EMA50<EMA200 و قیمت زیر EMA50",
                    "pts": round(reg_pts, 1)})
    vol_pts = float(np.clip(volz, 0, 2) * 2.5) if np.isfinite(volz) else 2.0
    mom_pts += vol_pts
    q_pts = 5.0 if 0.25 <= atr_pct <= 4.0 else 2.0
    reasons.append({"code": "volatility", "fa": f"نوسان 1H: ATR {atr_pct:.2f}٪",
                    "pts": round(q_pts, 1)})
    score = mom_pts + adx_pts + reg_pts + struct_pts + poi_pts + q_pts
    if abs(parts.get("1d", 0)) == 1 and parts.get("1d") != d:
        score -= 6.0
        warnings.append("ساختار روزانه خلاف جهت — ریسک بالاتر")
    score = float(np.clip(score, 0, 100))

    # ---- levels ----------------------------------------------------------
    entry = close - d * cfg.pullback_atr * atr
    sl = entry - d * cfg.sl_atr * atr
    risk = abs(entry - sl)

    pools: list[float] = []
    for tf in ("1h", "4h"):
        s2 = tfs.get(tf)
        if s2 is None or tf not in idx:
            continue
        for p in S.liquidity_targets(s2, idx[tf], d):
            if (p - entry) * d > risk:
                pools.append(p)
    pools = sorted(set(round(p, 10) for p in pools), key=lambda p: (p - entry) * d)

    tps = []
    for k, mult in enumerate(cfg.tp_r):
        snapped = entry + d * mult * risk
        for p in pools:
            r_mult = (p - entry) * d / risk
            if mult <= r_mult <= mult + 1.5:      # only nearby liquidity
                snapped = p
                break
        if tps and (snapped - tps[-1]) * d < 0.2 * risk:
            snapped = tps[-1] + d * 0.4 * risk
        tps.append(snapped)
    rr_marks = [abs(p - entry) / risk for p in tps]
    fee_abs = entry * cfg.fee_pct / 100.0
    rr = (abs(tps[1] - entry) - fee_abs) / (risk + fee_abs)

    # ---- leverage / sizing -----------------------------------------------
    sl_pct = risk / entry * 100.0
    lev_base = 20 if score >= 75 else 15 if score >= 60 else 10 if score >= 50 else 5
    lev_liq = 100.0 / (3.5 * max(sl_pct, 1e-6))
    lev = int(min(cfg.max_leverage, max(2.0, round(min(lev_base, lev_liq)))))
    notional_pct = cfg.risk_budget_pct / max(sl_pct, 1e-6) * 100.0
    margin_pct = notional_pct / lev

    grade = "A+" if score >= 78 else "A" if score >= 66 else "B" if score >= 55 else "C"
    signal = score >= cfg.min_score and rr >= cfg.min_rr

    return {
        **base_out,
        "signal": bool(signal),
        "veto": None if signal else ("low_rr" if score >= cfg.min_score else "low_score"),
        "direction": "LONG" if d > 0 else "SHORT",
        "setup": "MOMENTUM_CONTINUATION",
        "score": round(score, 1),
        "parts": {"mom": round(mom_pts, 1), "adx": round(adx_pts, 1),
                  "regime": round(reg_pts, 1), "struct": round(struct_pts, 1),
                  "poi": round(poi_pts, 1), "quality": round(q_pts, 1)},
        "grade": grade,
        "order_type": "LIMIT",
        "entry": _rnd(entry),
        "entry_market": _rnd(close),
        "entry_zone": [_rnd(entry - 0.25 * atr), _rnd(entry + 0.25 * atr)],
        "expiry_bars": cfg.pullback_valid,
        "sl": _rnd(sl),
        "sl_pct": round(sl_pct, 2),
        "tps": [{"label": f"TP{k+1}", "price": _rnd(p), "r": round(r, 2)}
                for k, (p, r) in enumerate(zip(tps, rr_marks))],
        "exit_policy": f"تریلینگ {cfg.trail_atr:.0f}×ATR روی 1H — TPها برای مدیریت دستی",
        "rr": round(rr, 2),
        "leverage": lev,
        "notional_pct": round(notional_pct, 1),
        "margin_pct": round(margin_pct, 1),
        "atr": _rnd(atr),
        "tf_rows": tf_rows,
        "poi": None if poi is None else {"kind": poi.kind, "src": poi_src,
                                         "top": _rnd(poi.top), "bot": _rnd(poi.bot),
                                         "mid": _rnd(poi.mid)},
        "sweep": None if sw1 is None else {"level": _rnd(sw1[1]), "bars_ago": i - sw1[0]},
        "range": {"low": _rnd(float(st.range_low_arr[i])) if st.range_low_arr is not None else None,
                  "high": _rnd(float(st.range_high_arr[i])) if st.range_high_arr is not None else None,
                  "pos": round(S.eq_position(st, i), 3)},
        "reasons": reasons,
        "warnings": warnings,
        "feat": {"adx1h": round(adx, 1), "adx4h": round(_v(tfs.get("4h"), "adx", idx.get("4h", 0)), 1),
                 "rsi1h": round(rsi, 1), "volz": round(volz, 2), "atr_pct": round(atr_pct, 2),
                 "pos": round(S.eq_position(st, i), 3), "bias": round(bias, 3),
                 "d1": parts.get("1d", 0), "h4": parts.get("4h", 0), "h1": parts.get("1h", 0),
                 "risk_pct": round(sl_pct, 3), "momz": round(momz, 2)},
        "ts": st.df.index[i].isoformat(),
    }
