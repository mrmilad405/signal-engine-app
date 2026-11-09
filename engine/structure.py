"""Market structure engine: swings, BOS / CHoCH, order blocks, FVG,
liquidity pools and stop-hunt (liquidity sweep) detection.

All functions are causal: a swing is only confirmed `k` bars after it forms and
every event is stored with the index at which it becomes known, so the live
scanner and the backtester see exactly the same information.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd


# --------------------------------------------------------------------------- #
# Swings
# --------------------------------------------------------------------------- #
def swing_pivots(df: pd.DataFrame, k: int = 2, confirm: bool = True):
    """Return (piv_high, piv_low) boolean arrays.

    A pivot is placed on the bar `k` bars *before* confirmation when
    ``confirm`` is True we additionally shift the "known at" index forward,
    which is handled by the caller through :func:`known_index`.
    """
    h, l = df["high"].to_numpy(), df["low"].to_numpy()
    n = len(df)
    ph = np.zeros(n, dtype=bool)
    pl = np.zeros(n, dtype=bool)
    for i in range(k, n - k):
        win_h = h[i - k: i + k + 1]
        win_l = l[i - k: i + k + 1]
        if h[i] == win_h.max() and h[i] > win_h[:k].max() and h[i] >= win_h[k + 1:].max():
            ph[i] = True
        if l[i] == win_l.min() and l[i] < win_l[:k].min() and l[i] <= win_l[k + 1:].min():
            pl[i] = True
    return ph, pl


@dataclass
class Zone:
    """A price zone (order block or fair value gap)."""
    idx: int                 # bar index where the zone becomes valid
    top: float
    bot: float
    dir: int                 # +1 = demand / bullish, -1 = supply / bearish
    kind: str                # "OB" | "FVG" | "OB+FVG"
    strength: float = 1.0    # displacement strength that created it
    touched: int | None = None   # first index price re-entered the zone
    filled: int | None = None    # first index price traded through the zone

    @property
    def mid(self) -> float:
        return (self.top + self.bot) / 2.0

    def contains(self, price: float) -> bool:
        return self.bot <= price <= self.top


@dataclass
class StructureState:
    """Container for everything the signal layer needs per timeframe."""
    df: pd.DataFrame
    trend: np.ndarray            # +1 bull, -1 bear, 0 undecided (per bar close)
    ev_dir: np.ndarray           # direction of the structural event on that bar
    ev_kind: np.ndarray          # "" | "BOS" | "CHoCH"
    ev_strength: np.ndarray      # displacement in ATR units
    sweep_dir: np.ndarray        # +1 sweep of lows (bullish), -1 sweep of highs
    sweep_level: np.ndarray
    zones: list = field(default_factory=list)
    last_high: float = np.nan    # last confirmed swing high price (final bar)
    last_low: float = np.nan
    range_high: float = np.nan   # dealing range extremes at the final bar
    range_low: float = np.nan
    # causal per-bar series (safe for backtesting)
    last_high_arr: np.ndarray | None = None
    last_low_arr: np.ndarray | None = None
    range_high_arr: np.ndarray | None = None
    range_low_arr: np.ndarray | None = None


# --------------------------------------------------------------------------- #
# Core builder
# --------------------------------------------------------------------------- #
def build(df: pd.DataFrame, k: int = 2, lookback_ob: int = 12,
          eq_tol: float = 0.18) -> StructureState:
    """Walk the frame once and produce the full structural picture."""
    n = len(df)
    o = df["open"].to_numpy()
    h = df["high"].to_numpy()
    l = df["low"].to_numpy()
    c = df["close"].to_numpy()
    atr = df["atr"].to_numpy()
    body = df["body"].to_numpy()

    ph, pl = swing_pivots(df, k)

    trend = np.zeros(n)
    ev_dir = np.zeros(n)
    ev_kind = np.full(n, "", dtype=object)
    ev_strength = np.zeros(n)
    sweep_dir = np.zeros(n)
    sweep_level = np.full(n, np.nan)
    last_hi_arr = np.full(n, np.nan)
    last_lo_arr = np.full(n, np.nan)
    rng_hi = np.full(n, np.nan)
    rng_lo = np.full(n, np.nan)

    zones: list[Zone] = []
    last_ph = np.nan
    last_pl = np.nan
    tr = 0
    swing_highs: list[tuple[int, float]] = []
    swing_lows: list[tuple[int, float]] = []
    active_highs: list[float] = []   # unbroken liquidity above
    active_lows: list[float] = []    # unbroken liquidity below

    for i in range(n):
        a = atr[i] if np.isfinite(atr[i]) and atr[i] > 0 else np.nan

        # ---- swing registration (confirmed k bars after the fact) ----------
        j = i - k
        if j >= k:
            if ph[j]:
                swing_highs.append((j, h[j]))
                active_highs.append(h[j])
                last_ph = h[j]
            if pl[j]:
                swing_lows.append((j, l[j]))
                active_lows.append(l[j])
                last_pl = l[j]

        # ---- structural break ----------------------------------------------
        if np.isfinite(last_ph) and c[i] > last_ph:
            kind = "BOS" if tr == 1 else "CHoCH"
            strength = (c[i] - last_ph) / a if np.isfinite(a) else 0.0
            if strength < 0.15 and not (body[i] > 1.1 * a):
                # weak poke: register but treat as low quality
                strength = min(strength, 0.2)
            ev_dir[i], ev_kind[i], ev_strength[i] = 1, kind, float(strength)
            tr = 1
            zones.append(_order_block(df, i, +1, lookback_ob, a))
            active_highs = [x for x in active_highs if x > c[i]]
            last_ph = np.nan
        elif np.isfinite(last_pl) and c[i] < last_pl:
            kind = "BOS" if tr == -1 else "CHoCH"
            strength = (last_pl - c[i]) / a if np.isfinite(a) else 0.0
            ev_dir[i], ev_kind[i], ev_strength[i] = -1, kind, float(strength)
            tr = -1
            zones.append(_order_block(df, i, -1, lookback_ob, a))
            active_lows = [x for x in active_lows if x < c[i]]
            last_pl = np.nan

        trend[i] = tr
        last_hi_arr[i] = last_ph
        last_lo_arr[i] = last_pl
        if swing_highs:
            rng_hi[i] = max(p for _, p in swing_highs[-8:])
        if swing_lows:
            rng_lo[i] = min(p for _, p in swing_lows[-8:])

        # ---- FVG (3-candle imbalance) --------------------------------------
        if i >= 2 and np.isfinite(a):
            if l[i] > h[i - 2] and (c[i - 1] - o[i - 1]) > 0.45 * a:
                zones.append(Zone(i, l[i], h[i - 2], +1, "FVG",
                                  strength=float((l[i] - h[i - 2]) / a)))
            if h[i] < l[i - 2] and (o[i - 1] - c[i - 1]) > 0.45 * a:
                zones.append(Zone(i, l[i - 2], h[i], -1, "FVG",
                                  strength=float((l[i - 2] - h[i]) / a)))

        # ---- zone lifecycle -------------------------------------------------
        for z in zones:
            if z.touched is None:
                if (z.dir > 0 and l[i] <= z.top) or (z.dir < 0 and h[i] >= z.bot):
                    z.touched = i
            if z.filled is None:
                if (z.dir > 0 and c[i] < z.bot) or (z.dir < 0 and c[i] > z.top):
                    z.filled = i

        # ---- liquidity sweep (stop hunt) ------------------------------------
        if np.isfinite(a):
            tol = eq_tol * a
            # sweep of highs -> bearish signal, sweep of lows -> bullish signal
            for lvl in reversed(active_highs[-6:]):
                if h[i] > lvl + 0.05 * a and c[i] < lvl and (h[i] - lvl) > 0.25 * a:
                    sweep_dir[i], sweep_level[i] = -1, lvl
                    active_highs = [x for x in active_highs if x != lvl]
                    break
            if sweep_dir[i] == 0:
                for lvl in reversed(active_lows[-6:]):
                    if l[i] < lvl - 0.05 * a and c[i] > lvl and (lvl - l[i]) > 0.25 * a:
                        sweep_dir[i], sweep_level[i] = +1, lvl
                        active_lows = [x for x in active_lows if x != lvl]
                        break
            del tol

    st = StructureState(
        df=df, trend=trend, ev_dir=ev_dir, ev_kind=ev_kind,
        ev_strength=ev_strength, sweep_dir=sweep_dir, sweep_level=sweep_level,
        zones=zones, last_high_arr=last_hi_arr, last_low_arr=last_lo_arr,
        range_high_arr=rng_hi, range_low_arr=rng_lo,
    )
    st.last_high = last_ph if np.isfinite(last_ph) else (swing_highs[-1][1] if swing_highs else np.nan)
    st.last_low = last_pl if np.isfinite(last_pl) else (swing_lows[-1][1] if swing_lows else np.nan)
    if swing_highs and swing_lows:
        recent_h = [p for idx, p in swing_highs[-8:]]
        recent_l = [p for idx, p in swing_lows[-8:]]
        st.range_high = float(max(recent_h))
        st.range_low = float(min(recent_l))
    return st


def _order_block(df: pd.DataFrame, i: int, direction: int, lookback: int,
                 atr_val: float) -> Zone:
    """Last opposite-colour candle before the impulsive break at bar ``i``."""
    o = df["open"].to_numpy()
    h = df["high"].to_numpy()
    l = df["low"].to_numpy()
    c = df["close"].to_numpy()
    start = max(0, i - lookback)
    pick = None
    for j in range(i - 1, start - 1, -1):
        if direction > 0 and c[j] < o[j]:
            pick = j
            break
        if direction < 0 and c[j] > o[j]:
            pick = j
            break
    if pick is None:
        pick = max(0, i - 1)
    if direction > 0:
        top, bot = max(o[pick], c[pick]), l[pick]
    else:
        top, bot = h[pick], min(o[pick], c[pick])
    strength = abs(c[i] - (l[pick] if direction > 0 else h[pick])) / atr_val if atr_val and np.isfinite(atr_val) else 1.0
    return Zone(i, float(top), float(bot), direction, "OB", strength=float(strength))


# --------------------------------------------------------------------------- #
# Helpers used by the signal layer
# --------------------------------------------------------------------------- #
def active_zones(st: StructureState, i: int, direction: int,
                 max_age: int = 300, include_filled: bool = False) -> list[Zone]:
    """Zones valid at bar ``i`` for the given trade direction, best first."""
    out = []
    zones = st.zones
    # zones are appended in chronological order -> walk backwards and stop early
    for idx in range(len(zones) - 1, -1, -1):
        z = zones[idx]
        if i - z.idx > max_age:
            break
        if z.idx > i or z.dir != direction:
            continue
        if not include_filled and z.filled is not None and z.filled <= i:
            continue
        out.append(z)
        if len(out) >= 12:
            break
    return out


def pois_near(st: StructureState, i: int, direction: int, price: float,
              atr_val: float, tol: float = 1.2, max_age: int = 300):
    """Zones overlapping the price area the entry needs (POI confluence)."""
    zs = active_zones(st, i, direction, max_age=max_age)
    keep = [z for z in zs if (z.bot - tol * atr_val) <= price <= (z.top + tol * atr_val)]
    # merge overlapping OB/FVG into a single high-quality POI
    best = None
    for z in keep[:8]:
        for w in keep[:8]:
            if z is w:
                continue
            if min(z.top, w.top) - max(z.bot, w.bot) > 0 and {z.kind, w.kind} == {"OB", "FVG"}:
                merged = Zone(min(z.idx, w.idx),
                              max(z.top, w.top), max(z.bot, w.bot) if direction > 0 else min(z.bot, w.bot),
                              direction, "OB+FVG", max(z.strength, w.strength))
                if best is None or merged.strength > best.strength:
                    best = merged
    if best is not None:
        return [best] + [z for z in keep if z.kind != "OB+FVG"]
    return keep


def eq_position(st: StructureState, i: int) -> float:
    """0 = range low, 1 = range high (premium/discount location, causal)."""
    if st.range_high_arr is not None:
        hi, lo = float(st.range_high_arr[i]), float(st.range_low_arr[i])
    else:
        lo, hi = st.range_low, st.range_high
    if not (np.isfinite(lo) and np.isfinite(hi)) or hi <= lo:
        return 0.5
    return float((st.df["close"].iloc[i] - lo) / (hi - lo))


def liquidity_targets(st: StructureState, i: int, direction: int) -> list[float]:
    """Opposing liquidity that can serve as take-profit magnets."""
    out = []
    lh = float(st.last_high_arr[i]) if st.last_high_arr is not None else st.last_high
    ll = float(st.last_low_arr[i]) if st.last_low_arr is not None else st.last_low
    if direction > 0 and np.isfinite(lh):
        out.append(lh)
    if direction < 0 and np.isfinite(ll):
        out.append(ll)
    # unmitigated opposing zones
    for z in active_zones(st, i, -direction if direction else 0, max_age=400):
        out.append(z.mid if direction > 0 else z.mid)
        if len(out) > 6:
            break
    return sorted(set(round(x, 8) for x in out if np.isfinite(x)))
